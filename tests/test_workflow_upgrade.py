import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from crashcustomer import tracing
from crashcustomer.browser import BrowserTools
from crashcustomer.campaign import CampaignManager, CampaignRequest, build_candidates
from crashcustomer.events import encode_snapshot, event_frames
from crashcustomer.mcp_context import PlanningContext
from crashcustomer.probes import load_probe, validation_probe
from crashcustomer.secrets import scrub
from crashcustomer.store import Store
from crashcustomer.validation import validate_campaign


def project(store):
    return store.create_project({"name": "test", "browser_url": "http://localhost:8720/shop"})


def executor(result="goal_completed", seen=None):
    async def run(store, run_id, auth_headers=None):
        r = store.get_run(run_id)
        if seen is not None:
            seen.append({"faults": r["faults"], "headers": auth_headers})
        r.update(status="completed", result=result, elapsed_seconds=.01,
                 requests=[{"method": "POST", "path": "/api/orders", "body": {"name": "test"}}],
                 injected=r["faults"])
        store.save_run(r)
    return run


async def execute(manager, p, **values):
    record = manager.start(p, CampaignRequest(**values))
    await manager.tasks[record["id"]]
    return manager.store.get_campaign(record["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["inconclusive", "budget_exhausted", "findings_confirmed"])
async def test_baseline_gate_stops_faults_and_load(tmp_path, result):
    store, seen = Store(tmp_path), []
    manager = CampaignManager(lambda: store, asyncio.Semaphore(1), run_executor=executor(result, seen))
    record = await execute(manager, project(store))
    assert record["status"] == "inconclusive"
    assert not record["gate"]["passed"] and len(seen) == 1
    assert record["probes"] is None and not record["scenarios"]
    assert record["validation"]["issues"]


@pytest.mark.asyncio
async def test_mcp_prompt_resource_and_search_are_real(tmp_path):
    store = Store(tmp_path)
    p = project(store)
    store.add_attachment(p["id"], "runbook.md", b"orders fail retry with backoff", {"category": "reference", "format": "md"})
    async with PlanningContext(tmp_path) as context:
        result = await context.context(p["id"], "orders test", "orders retry")
        check = await context.call("check_plan_coverage", {"candidates": [{"candidate_id": "1", "fault": "delay"}], "scenarios": [], "limit": 1})
    assert "장애 유형별" in result["prompt"]
    assert result["runbook"]["sources"][0]["filename"] == "runbook.md"
    assert result["references"][0]["source"] == "runbook.md"
    assert check["missing"] == ["delay"]


@pytest.mark.asyncio
async def test_bounded_replan_repairs_omitted_faults(tmp_path):
    store = Store(tmp_path)
    calls = []

    class IncompletePlanner:
        async def plan(self, goal, candidates, references, limit):
            calls.append(1)
            return [{**candidates[0], "reason": "only one"}], {}

    manager = CampaignManager(lambda: store, asyncio.Semaphore(1),
                              planner_factory=lambda _: IncompletePlanner(), run_executor=executor())
    record = await execute(manager, project(store), api_checks=False, load_test=False)
    assert len(calls) == 2 and record["plan_attempts"] == 2
    assert not record["coverage"]["missing"] and record["coverage"]["repaired"]
    assert any(s.get("source") == "coverage_repair" for s in record["scenarios"])
    assert record["status"] == "completed", record
    assert record["mcp"]["calls"].count("tools/call:check_plan_coverage") == 2


@pytest.mark.asyncio
async def test_general_help_does_not_execute_browser(tmp_path):
    store, seen = Store(tmp_path), []
    manager = CampaignManager(lambda: store, asyncio.Semaphore(1), run_executor=executor(seen=seen))
    r = await execute(manager, project(store), goal="사용법 알려줘")
    assert r["verdict"] == "guidance" and r["answer"] and not seen


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 302])
async def test_invalid_preflight_never_becomes_good_load(status):
    hits = []
    def handler(request):
        hits.append(request)
        return httpx.Response(status, json={"error": "auth"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as c:
        result = await load_probe(c, ["/private"], [1, 10], 1000)
    assert not result["valid"] and not result["stages"] and not result["findings"]
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_auth_expiration_during_load_invalidates_measurement():
    hits = 0
    def handler(request):
        nonlocal hits
        hits += 1
        return httpx.Response(200 if hits == 1 else 401, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as c:
        r = await load_probe(c, ["/private"], [1], 1000)
    assert not r["valid"] and r["stages"][0]["error_rate"] == 1 and not r["findings"]


@pytest.mark.asyncio
async def test_html_login_page_is_not_valid_json_api():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="login", headers={"content-type": "text/html"})), base_url="http://test") as c:
        r = await load_probe(c, ["/api"], [1], 1000, expect_json_paths=["/api"])
    assert not r["valid"] and not r["stages"]


@pytest.mark.asyncio
async def test_token_used_for_browser_and_probe_but_never_saved(tmp_path):
    token = "synthetic-test-credential"
    store, seen, headers = Store(tmp_path), [], []
    def client(base):
        def handler(request):
            headers.append(request.headers.get("Authorization"))
            return httpx.Response(200, json={"ok": True})
        return httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(handler))
    manager = CampaignManager(lambda: store, asyncio.Semaphore(1), run_executor=executor(seen=seen), probe_client_factory=client)
    r = await execute(manager, project(store), test_token=token, api_checks=False, load_max_users=1)
    assert r["config"]["authenticated"] and "test_token" not in r["config"]
    assert headers and set(headers) == {"Bearer " + token}
    assert all(s["headers"] == {"Authorization": "Bearer " + token} for s in seen)
    assert not manager.auth_headers
    assert token not in json.dumps(r)
    with store.connect() as conn:
        assert token not in "".join(row[0] for row in conn.execute("SELECT payload FROM runs"))
    browser = BrowserTools("http://localhost/shop", tmp_path, "id", "phase", auth_headers={"Authorization": "Bearer " + token})
    assert not browser.trace_enabled


def test_token_header_injection_is_rejected():
    with pytest.raises(ValidationError):
        CampaignRequest(test_token="bad\r\nHeader: injected")


@pytest.mark.asyncio
async def test_sse_resume_deduplicates_snapshot_and_closes_at_terminal():
    record = {"id": "1", "status": "running", "events": []}
    async def connected(): return False
    frames = event_frames(lambda: record, connected, interval=0)
    first = await anext(frames)
    assert "event: update" in first and "id: " in first
    event_id, _ = encode_snapshot(record)
    await frames.aclose()
    resumed = event_frames(lambda: record, connected, last_id=event_id, interval=0)
    record.update(status="completed", events=[{"stage": "finalize"}])
    next_frame = await anext(resumed)
    assert "finalize" in next_frame and event_id not in next_frame
    assert "event: done" in await anext(resumed)
    with pytest.raises(StopAsyncIteration): await anext(resumed)
    terminal_id, _ = encode_snapshot(record)
    frames = event_frames(lambda: record, connected, last_id=terminal_id, interval=0)
    assert "event: done" in await anext(frames)


@pytest.mark.asyncio
async def test_sse_disconnect_stops_without_loading_record():
    async def disconnected(): return True
    frames = event_frames(lambda: pytest.fail("read after disconnect"), disconnected)
    with pytest.raises(StopAsyncIteration): await anext(frames)


@pytest.mark.asyncio
async def test_tracing_unconfigured_does_not_send(tmp_path, monkeypatch):
    monkeypatch.setattr(tracing, "tracing_enabled", lambda: False)
    r = await tracing.verify_delivery(tmp_path, client_factory=lambda **_: pytest.fail("external send"))
    assert r["status"] == "not_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize("match", [True, False])
async def test_tracing_requires_readback_evidence(tmp_path, monkeypatch, match):
    monkeypatch.setattr(tracing, "tracing_enabled", lambda: True)
    class FakeClient:
        def __init__(self, **kwargs): pass
        def create_run(self, *args, **kwargs): self.id = kwargs["id"]
        def update_run(self, run_id, **kwargs): self.outputs = kwargs["outputs"]
        def read_run(self, run_id): return SimpleNamespace(id=run_id, outputs=self.outputs if match else {})
        def get_run_url(self, **kwargs): return "https://trace.test/synthetic"
        def close(self): pass
    r = await tracing.verify_delivery(tmp_path, client_factory=FakeClient, waits=(0, 0))
    assert r["status"] == ("verified" if match else "not_verified")
    assert json.loads((tmp_path / "tracing-verification.json").read_text())["run_id"] == r["run_id"]


def test_report_validator_rejects_invalid_measurement():
    record = {"scenarios": [], "probes": {"load": {"valid": False}}}
    r = validate_campaign(record, [{"result": "goal_completed"}])
    assert r["verdict"] == "partial" and any("무효" in i for i in r["issues"])
    assert scrub({"Authorization": "secret", "body": "a secret"}, ["secret"]) == {"Authorization": "[redacted]", "body": "a [redacted]"}


@pytest.mark.asyncio
async def test_input_validation_redirect_is_not_a_pass():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(302)), base_url="http://test") as client:
        r = await validation_probe(client, [{"method": "POST", "path": "/private", "body": {"name": "test"}}])
    assert not r["valid"] and not r["findings"]


@pytest.mark.asyncio
async def test_validation_rechecks_stay_inside_request_budget():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400 if len(calls) == 1 else 500)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        r = await validation_probe(client, [{"method": "POST", "path": "/orders", "body": {str(i): "v" for i in range(20)}}])
    assert len(calls) == r["requests"] == 30
    assert r["unverified_5xx"] == 1


def test_report_validator_rejects_invented_summary_counts():
    r = validate_campaign({"scenarios": [], "summary": {"scenarios": 99, "findings": 99, "confirmed": 99}}, [{"result": "goal_completed"}])
    assert any("요약 수치" in issue for issue in r["issues"])
