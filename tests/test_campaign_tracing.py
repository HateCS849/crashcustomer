from types import SimpleNamespace as Run

import pytest

from crashcustomer import tracing


def client_factory(complete=True):
    class Client:
        def __init__(self, **kwargs): pass
        def list_runs(self, **kwargs):
            if kwargs.get("is_root"):
                return [Run(id="root", trace_id="trace", extra={"metadata": {"run_id": "abc"}}, end_time=True, error=None, url="https://trace.test/private")]
            nodes = [Run(name="explorer.decide", run_type="llm", end_time=True, error=None)]
            if complete: nodes.append(Run(name="mcp.context", run_type="tool", end_time=True, error=None))
            return nodes
        def close(self): pass
    return Client


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_actual_campaign_requires_llm_and_mcp_children(tmp_path, monkeypatch, complete):
    monkeypatch.setattr(tracing, "tracing_enabled", lambda: True)
    r = await tracing.verify_campaign_delivery(tmp_path, {"id": "abc"}, client_factory=client_factory(complete))
    assert r["status"] == ("verified" if complete else "incomplete_trace")
    assert r["campaign_id"] == "abc" and r["completed_llm_spans"] == 1
    assert (tmp_path / "campaign-traces" / "abc.json").exists()


@pytest.mark.asyncio
async def test_actual_campaign_missing_key_does_not_query_cloud(tmp_path, monkeypatch):
    monkeypatch.setattr(tracing, "tracing_enabled", lambda: False)
    r = await tracing.verify_campaign_delivery(tmp_path, {"id": "abc"}, client_factory=lambda **_: pytest.fail("unexpected cloud call"))
    assert r["status"] == "not_configured"


def test_full_and_compact_keep_same_allowed_actions_and_hide_fault_metadata():
    from crashcustomer.policy import OllamaPolicy
    snapshot = {"text": "x"*2000, "aria": "observed DOM", "screenshot": "/screen", "injected": [{"mode": "status"}]}
    actions = [{"kind": "fill", "target": "name", "value": str(i)} for i in range(8)]
    full, full_history = OllamaPolicy(compact_context=False).context(snapshot, actions)
    compact, history = OllamaPolicy().context(snapshot, actions)
    assert len(full["text"]) == 2000 and len(compact["text"]) == 1500
    assert len(full_history) == 8 and len(history) == 5
    assert "injected" not in full and "injected" not in compact


def test_type_coverage_does_not_hide_missing_mutation_scope():
    from crashcustomer.validation import coverage, complete_plan
    candidates = [{"candidate_id": "g", "fault": "status_401", "method": "GET", "path": "/stock"},
                  {"candidate_id": "p", "fault": "status_401", "method": "POST", "path": "/orders"}]
    check = coverage(candidates, [candidates[0]], 1)
    assert check["planned"] == ["status_401"] and check["missing"] == ["status_401"]
    repaired = complete_plan(candidates, [candidates[0]], 1)
    assert repaired[0]["method"] == "POST" and not coverage(candidates, repaired, 1)["missing"]
