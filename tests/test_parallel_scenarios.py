import asyncio
import time

import pytest

from crashcustomer.campaign import CampaignManager, CampaignRequest
from crashcustomer.store import Store


def project(store):
    return store.create_project({"name": "test", "browser_url": "http://localhost:8720/shop"})


def slow_executor(state, delay=.15, fail_label=None):
    async def run(store, run_id, auth_headers=None):
        record = store.get_run(run_id)
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            if record["faults"]:
                if record["label"] == fail_label:
                    raise RuntimeError("browser crashed")
                await asyncio.sleep(delay)
            record.update(status="completed", result="goal_completed", elapsed_seconds=delay if record["faults"] else .01,
                          requests=[{"method": "POST", "path": "/api/orders", "body": {"name": "test"}}], injected=record["faults"])
            store.save_run(record)
        finally:
            state["active"] -= 1
    return run


async def execute(manager, store, **values):
    record = manager.start(project(store), CampaignRequest(api_checks=False, load_test=False, max_replans=0, **values))
    started = time.monotonic()
    await manager.tasks[record["id"]]
    return store.get_campaign(record["id"]), time.monotonic() - started


@pytest.mark.asyncio
@pytest.mark.parametrize("width,slots,expected", [(1, 2, 1), (2, 2, 2), (4, 2, 2), (3, 3, 3)])
async def test_scenarios_run_with_bounded_width(tmp_path, width, slots, expected):
    store, state = Store(tmp_path), {"active": 0, "peak": 0}
    manager = CampaignManager(lambda: store, asyncio.Semaphore(slots), run_executor=slow_executor(state), browser_slots=slots)
    campaign, _ = await execute(manager, store, max_scenarios=6, scenario_concurrency=width)
    assert state["peak"] == expected
    assert campaign["timings"]["scenario_concurrency"] == expected
    assert len(campaign["scenarios"]) == 6 and all(s["status"] == "completed" for s in campaign["scenarios"])


@pytest.mark.asyncio
async def test_parallel_keeps_plan_order_and_is_faster(tmp_path):
    timings = {}
    for width in (1, 3):
        store, state = Store(tmp_path / str(width)), {"active": 0, "peak": 0}
        manager = CampaignManager(lambda: store, asyncio.Semaphore(3), run_executor=slow_executor(state, delay=.2), browser_slots=3)
        campaign, _ = await execute(manager, store, max_scenarios=6, scenario_concurrency=width)
        runs = [store.get_run(s["run_id"]) for s in campaign["scenarios"]]
        assert [r["label"] for r in runs] == [s["label"] for s in campaign["scenarios"]]
        assert campaign["run_ids"][0] != campaign["scenarios"][0]["run_id"]  # baseline stays first
        timings[width] = campaign["timings"]["attack_seconds"]
    assert timings[3] < timings[1] * .6


@pytest.mark.asyncio
async def test_one_failing_scenario_fails_the_campaign_without_hanging(tmp_path):
    store, state = Store(tmp_path), {"active": 0, "peak": 0}
    probe = CampaignManager(lambda: store, asyncio.Semaphore(2), run_executor=slow_executor(state), browser_slots=2)
    planned, _ = await execute(probe, store, max_scenarios=4, scenario_concurrency=2)
    victim = planned["scenarios"][1]["label"]
    store2, state2 = Store(tmp_path / "fail"), {"active": 0, "peak": 0}
    manager = CampaignManager(lambda: store2, asyncio.Semaphore(2), run_executor=slow_executor(state2, fail_label=victim), browser_slots=2)
    campaign, _ = await asyncio.wait_for(execute(manager, store2, max_scenarios=4, scenario_concurrency=2), 10)
    assert campaign["status"] == "error" and "browser crashed" in campaign["error"]
    assert state2["active"] == 0
