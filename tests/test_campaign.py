import asyncio
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import crashcustomer.app as app_module
from crashcustomer.browser import BrowserTools
from crashcustomer.campaign import CampaignManager, OllamaPlanner, RulePlanner, build_candidates
from crashcustomer.checks import check_resilience
from crashcustomer.retrieval import Retriever, attachment_chunks, tokenize
from crashcustomer.store import Store
from crashcustomer.targets import TargetError, check_target, health_check


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:8720/buggy", True), ("http://localhost:3000/", True), ("http://shop.localhost/", True),
    ("http://api.test/", True), ("https://www.example.com/", False), ("http://10.0.0.5/", False),
    ("http://127.0.0.1:8710/", False), ("", False), ("ftp://localhost/", False),
])
def test_only_local_or_allowlisted_hosts_can_be_targets(url, ok):
    if ok:
        assert check_target(url) == url
    else:
        with pytest.raises(TargetError):
            check_target(url)


def test_staging_host_requires_explicit_allowlist(monkeypatch):
    with pytest.raises(TargetError, match="CRASHCUSTOMER_ALLOWED_HOSTS"):
        check_target("https://staging.example.com/")
    monkeypatch.setenv("CRASHCUSTOMER_ALLOWED_HOSTS", "staging.example.com")
    assert check_target("https://staging.example.com/")


async def test_health_check_reports_unreachable_app():
    transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await health_check("http://127.0.0.1:8720/", client)
    assert result["ok"] is False and "ConnectError" in result["message"]


def request(url, method="POST"):
    return SimpleNamespace(url=url, method=method, resource_type="fetch")


def test_origin_scope_allows_same_origin_and_faults_fire_once():
    tools = BrowserTools("http://127.0.0.1:8720/buggy", Path("/tmp/unused"), "t", "explore", scope="origin",
                         faults=[{"method": "POST", "path": "/api/buggy/orders", "mode": "status", "status": 503, "times": 1}])
    assert tools.allowed("http://127.0.0.1:8720/api/anything")
    assert not tools.allowed("http://127.0.0.1:9999/api/anything")
    assert not tools.allowed("https://cdn.example.com/lib.js")
    rule = tools.matching_fault(request("http://127.0.0.1:8720/api/buggy/orders"))
    assert rule and tools.matching_fault(request("http://127.0.0.1:8720/api/buggy/orders", "GET")) is None
    rule["remaining"] -= 1
    assert tools.matching_fault(request("http://127.0.0.1:8720/api/buggy/orders")) is None


def snap(**extra):
    base = {"step": 2, "screenshot": "/s.png", "network": [], "statuses": [], "alerts": [], "busy": False, "text": "주문\n주문하기",
            "error_count": 0, "fields": [{"label": "이메일", "type": "email", "value": "qa@example.com", "checked": False}],
            "buttons": [{"label": "주문하기", "type": "submit", "disabled": False}]}
    return {**base, **extra}


CLICK = {"kind": "click"}
FAILED = [{"method": "POST", "status": 503}]


def codes(before, after):
    return sorted(f["code"] for f in check_resilience(before, after, CLICK))


def test_handled_failure_is_not_a_defect():
    assert codes(snap(), snap(network=FAILED, alerts=["일시적인 오류입니다. 다시 시도해주세요."])) == []


def test_silent_failure_and_input_loss_require_failed_request():
    after = snap(network=FAILED)
    after["fields"][0]["value"] = ""
    assert codes(snap(), after) == ["input_loss", "silent_failure"]
    assert codes(snap(), snap()) == []


def test_false_success_uses_newly_shown_text():
    assert codes(snap(), snap(network=FAILED, statuses=["주문이 완료되었습니다."])) == ["false_success"]
    static = snap(text="결제 완료 후 메일이 발송됩니다\n주문하기")
    assert "false_success" not in codes(static, {**copy.deepcopy(static), "network": FAILED, "alerts": ["오류"]})


def test_aborted_request_with_script_exception():
    after = snap(network=[{"method": "POST", "status": 0}], error_count=1, console_errors=["TypeError: Failed to fetch"])
    assert codes(snap(), after) == ["silent_failure", "unhandled_error"]


def test_double_submit_detected_only_when_button_stays_usable():
    assert codes(snap(), snap(mid_flight=[{"label": "주문하기", "enabled": True, "busy": False}])) == ["double_submit"]
    assert codes(snap(), snap(mid_flight=[{"label": "주문하기", "enabled": False, "busy": True}])) == []


def test_korean_tokenizer_uses_bigrams():
    assert {"중복", "복제", "제출", "post"} <= set(tokenize("중복제출 POST"))


ATTACHMENTS = [
    {"id": "a", "filename": "api.yaml", "category": "openapi", "format": "yaml", "summary": {"operations": [
        {"method": "POST", "path": "/api/orders", "summary": "주문 생성", "parameters": [], "authenticated": False},
        {"method": "GET", "path": "/api/orders/count", "summary": "주문 수", "parameters": [], "authenticated": True}]}},
    {"id": "b", "filename": "runbook.md", "category": "reference", "format": "md", "summary": {}},
    {"id": "c", "filename": "photo.pdf", "category": "reference", "format": "pdf", "summary": {}},
]
TEXTS = {"b": "# 운영\n\n결제 대행사 지연 시 고객이 주문 버튼을 여러 번 눌러 중복 결제가 발생했습니다.\n\n로그 보관 정책은 30일입니다."}


async def test_bm25_retrieves_relevant_runbook_section():
    chunks = attachment_chunks(ATTACHMENTS, TEXTS.get)
    assert {c["source"] for c in chunks} == {"api.yaml", "runbook.md"}
    results = await Retriever(chunks).search("주문 지연 중복 결제", k=2)
    assert results[0]["source"] == "runbook.md" and results[0]["method"] == "bm25"


async def test_hybrid_retrieval_uses_injected_embedder():
    calls = []

    async def embed(texts):
        calls.append(len(texts))
        return [[1.0, 0.0] if "count" in t or "주문 수" in t else [0.0, 1.0] for t in texts]
    results = await Retriever(attachment_chunks(ATTACHMENTS, TEXTS.get), embed).search("주문 수 count", k=1)
    assert results[0]["method"] == "hybrid" and "count" in results[0]["text"]
    assert calls[0] == 3


def test_candidates_come_only_from_observed_requests():
    candidates = build_candidates([{"method": "POST", "path": "/api/orders"}, {"method": "GET", "path": "/api/items"}])
    post = {c["fault"] for c in candidates if c["method"] == "POST"}
    get = {c["fault"] for c in candidates if c["method"] == "GET"}
    assert post == {"status_503", "abort", "delay", "status_401", "status_429", "mutate", "duplicate", "status_500"}
    assert get == {"status_503", "delay", "status_401", "status_429", "mutate", "persistent_503", "status_500"}
    assert [c["fault"] for c in candidates if c["method"] == "*"] == ["slow_network"]
    assert {c["path"] for c in candidates if c["method"] != "*"} == {"/api/orders", "/api/items"}
    with_assets = build_candidates([{"method": "GET", "path": "/api/items"}], [{"type": "script", "path": "/app.js"}])
    assert "asset_block" in {c["fault"] for c in with_assets}
    assert build_candidates([]) == []


async def test_rule_planner_covers_each_fault_type_before_repeating():
    candidates = build_candidates([{"method": "GET", "path": "/api/items"}, {"method": "POST", "path": "/api/orders"}],
                                  [{"type": "script", "path": "/app.js"}])
    chosen, usage = await RulePlanner().plan("주문", candidates, [], 11)
    assert len({c["fault"] for c in chosen}) == 11 and usage == {}
    assert next(c for c in chosen if c["fault"] == "status_503")["method"] == "POST"
    assert next(c for c in chosen if c["fault"] == "persistent_503")["method"] == "GET"
    more, _ = await RulePlanner().plan("주문", candidates, [], 13)
    assert [c["fault"] for c in more[11:]] == ["status_503", "delay"]


async def test_llm_planner_is_constrained_to_candidate_ids(monkeypatch):
    original = httpx.AsyncClient
    candidates = build_candidates([{"method": "POST", "path": "/api/orders"}])
    duplicate_id = next(c["candidate_id"] for c in candidates if c["fault"] == "duplicate")

    def handler(req):
        payload = json.loads(req.content)
        schema = payload["format"]
        assert schema["$defs"]["PlannerChoice"]["properties"]["candidate_id"]["enum"] == [c["candidate_id"] for c in candidates]
        prompt = json.loads(payload["messages"][1]["content"])
        assert prompt["references"][0]["source"] == "runbook.md"
        return httpx.Response(200, json={"message": {"content": json.dumps({"scenarios": [
            {"candidate_id": duplicate_id, "reason": "지연 시 중복 결제 이력"}, {"candidate_id": duplicate_id, "reason": "중복"}]})},
            "prompt_eval_count": 300, "eval_count": 40})
    monkeypatch.setattr("crashcustomer.campaign.httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    chosen, usage = await OllamaPlanner().plan("주문", candidates, [{"source": "runbook.md", "text": "중복 결제"}], 2)
    assert [c["fault"] for c in chosen] == ["duplicate"] and usage["calls"] == 1


@pytest.fixture
def campaign_client(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(app_module, "store", store)
    executed = []

    async def fake_run(store_, run_id):
        run = store_.get_run(run_id)
        executed.append(run["faults"])
        run.update(status="completed", elapsed_seconds=.1, result="goal_completed",
                   requests=[{"method": "POST", "path": "/api/orders", "body": {"quantity": 1}}] if not run["faults"] else [])
        if run["faults"] and run["faults"][0]["mode"] == "abort":
            run["findings"] = [{"code": "silent_failure", "title": "실패 무응답", "severity": "medium", "verification": "confirmed",
                                "expected": "-", "actual": "-", "before": "/b", "after": "/a"}]
        store_.save_run(run)

    target = FastAPI()

    @target.post("/api/orders")
    async def orders(request: Request):
        body = await request.json()
        return {"id": int(body["quantity"])}

    @target.get("/shop")
    def shop():
        return {"ok": True}
    probe_factory = lambda base: httpx.AsyncClient(transport=httpx.ASGITransport(app=target, raise_app_exceptions=False), base_url=base)
    manager = CampaignManager(lambda: store, asyncio.Semaphore(1), run_executor=fake_run, probe_client_factory=probe_factory)
    monkeypatch.setattr(app_module, "campaign_manager", manager)

    async def healthy(url, client=None):
        return {"ok": True, "status": 200, "latency_ms": 1, "message": "ok"}
    monkeypatch.setattr(app_module, "health_check", healthy)
    with TestClient(app_module.app) as client:
        yield client, executed


def test_campaign_runs_baseline_then_planned_faults(campaign_client):
    client, executed = campaign_client
    blocked = client.post("/api/projects", json={"name": "운영", "browser_url": "https://shop.example.com/"}).json()
    assert blocked["execution"]["status"] == "blocked"
    assert client.post(f"/api/projects/{blocked['id']}/campaigns", json={}).status_code == 400
    project = client.post("/api/projects", json={"name": "샵", "browser_url": "http://127.0.0.1:8720/shop"}).json()
    response = client.post(f"/api/projects/{project['id']}/campaigns", json={"max_scenarios": 3, "load_max_users": 2})
    assert response.status_code == 202
    for _ in range(200):
        campaign = client.get(f"/api/campaigns/{response.json()['id']}").json()
        if campaign["status"] not in {"queued", "running"}:
            break
        time.sleep(.02)
    assert campaign["status"] == "partial", campaign.get("error")
    assert executed[0] == [] and [rule[0]["mode"] for rule in executed[1:]] == ["status", "abort", "delay"]
    codes = {f["code"] for f in campaign["probes"]["findings"]}
    assert codes == {"input_validation_500"}
    assert [stage["users"] for stage in campaign["probes"]["load"]["stages"]] == [1, 2]
    assert campaign["probes"]["load"]["paths"] == ["/shop"]
    assert campaign["summary"]["confirmed"] == 2 and campaign["summary"]["baseline_completed"]
    assert [e["stage"] for e in campaign["events"]][:3] == ["route", "recon", "gate"]
    report = client.get(f"/api/campaigns/{campaign['id']}/report").text
    assert "실패 무응답" in report and "연결 끊김" in report and "단계별 부하" in report
