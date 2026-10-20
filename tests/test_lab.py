import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import crashcustomer.app as app_module
from crashcustomer.lab import LabManager, LabRequest
from crashcustomer.lab_service import ConnectionPool, PoolTimeout
from crashcustomer.store import Store


@pytest.fixture
def lab_client(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(app_module, "store", store)
    factory = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://testserver")
    manager = LabManager(lambda: store, client_factory=factory)
    monkeypatch.setattr(app_module, "lab_manager", manager)
    with TestClient(app_module.app) as client:
        yield client, manager, store


def await_run(client, run_id):
    for _ in range(200):
        response = client.get(f"/api/lab/runs/{run_id}")
        run = response.json()
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(.02)
    raise AssertionError("Experiment did not finish")


def test_http_contention_and_isolation_are_measured(lab_client):
    client, manager, store = lab_client
    config = {"users": 8, "pool_size": 2, "external_delay_ms": 250, "external_timeout_ms": 60,
              "pool_acquire_ms": 50, "login_slo_ms": 400, "log_queue_capacity": 1, "log_write_ms": 20}
    response = client.post("/api/lab/runs", json=config)
    assert response.status_code == 202
    run = await_run(client, response.json()["id"])
    assert run["status"] == "completed", run.get("error")
    before, after = run["cases"]
    assert len(before["requests"]) == len(after["requests"]) == 10  # 8 login + 2 explicit observers
    assert before["summary"]["protected_reads_passed"] is False
    assert before["final"]["pool"]["acquisition_timeouts"] > 0
    assert before["final"]["pool"]["hold_max_ms"]["login"] >= 200
    assert after["summary"]["protected_reads_passed"] is True
    assert after["summary"]["login_slo_passed"] is True
    assert after["final"]["pool"]["acquisition_timeouts"] == 0
    logs = after["final"]["logs"]
    assert logs["file_saved"] > 0
    assert logs["peak_depth"] <= config["log_queue_capacity"]
    assert logs["received"] == logs["db_saved"] + logs["file_saved"] == 8
    assert after["final"]["pool"]["active"] == 0
    assert manager.sessions == {}
    path = store.artifacts / "lab" / run["id"] / "after" / "fallback.jsonl"
    assert len(path.read_text().splitlines()) == logs["file_saved"]
    assert client.get(f"/api/lab/runs/{run['id']}/report").status_code == 200
    assert client.get(f"/api/lab/runs/{run['id']}/report?format=json").json()["config"]["users"] == 8
    assert client.get(f"/api/lab/runs/{run['id']}/logs/after").status_code == 200


def test_no_fault_does_not_invent_a_failure(lab_client):
    client, _, _ = lab_client
    response = client.post("/api/lab/runs", json={"users": 3, "pool_size": 10, "external_delay_ms": 0, "comparison": "before", "log_write_ms": 0})
    run = await_run(client, response.json()["id"])
    assert run["status"] == "completed", run.get("error")
    case = run["cases"][0]
    assert all(r["status"] == 200 for r in case["requests"])
    assert case["summary"]["protected_reads_passed"] is True
    assert not any(f["severity"] == "failure" for f in run["findings"])


def test_cancellation_cleans_up_live_resources(lab_client):
    client, manager, store = lab_client
    response = client.post("/api/lab/runs", json={"users": 5, "pool_size": 2, "external_delay_ms": 1000})
    run_id = response.json()["id"]
    for _ in range(50):
        if manager.sessions:
            break
        time.sleep(.01)
    response = client.post(f"/api/lab/runs/{run_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert manager.sessions == {}
    assert store.get_experiment(run_id)["finished_at"]


def test_model_failure_does_not_discard_measurements(lab_client, monkeypatch):
    client, _, _ = lab_client
    async def fail(_):
        raise TimeoutError("injected AI outage")
    monkeypatch.setattr("crashcustomer.lab.ai_analysis", fail)
    response = client.post("/api/lab/runs", json={"users": 1, "pool_size": 5, "external_delay_ms": 0, "comparison": "after", "analysis_backend": "ollama"})
    run = await_run(client, response.json()["id"])
    assert run["status"] == "completed"
    assert run["ai_error"] and run["cases"][0]["summary"]


@pytest.mark.parametrize("values", [{"users": 0}, {"users": 201}, {"pool_size": 0}, {"external_delay_ms": 999999}, {"external_timeout_ms": -1}, {"comparison": "remote"}, {"url": "http://example.com"}])
def test_experiments_are_bounded_and_cannot_target_arbitrary_urls(lab_client, values):
    client, _, _ = lab_client
    assert client.post("/api/lab/runs", json=values).status_code == 422


async def test_pool_lease_is_returned_after_cancellation(tmp_path):
    pool = ConnectionPool(tmp_path / "test.sqlite3", 1, 50)
    entered = asyncio.Event()
    async def hold():
        async with pool.lease("login:cancelled"):
            entered.set()
            await asyncio.sleep(10)
    task = asyncio.create_task(hold())
    await entered.wait()
    with pytest.raises(PoolTimeout):
        async with pool.lease("read:waiting"):
            pass
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert pool.snapshot()["active"] == 0
    assert pool.snapshot()["idle"] == 1
    assert pool.snapshot()["waiting"] == 0
    pool.close()
