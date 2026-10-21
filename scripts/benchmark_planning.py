"""Paired one-pass vs validated planning; actual model calls, frozen observed API candidates."""
import argparse
import asyncio
import json
import time
from pathlib import Path

from crashcustomer.campaign import OllamaPlanner, build_candidates
from crashcustomer.config import DATA_DIR, OLLAMA_MODEL
from crashcustomer.mcp_context import PlanningContext
from crashcustomer.store import Store
from crashcustomer.validation import complete_plan, coverage


async def benchmark(repeats, output):
    store = Store(DATA_DIR)
    previous = json.loads(Path("docs/evaluation-workflow-20261021.json").read_text())
    row = next(r for r in previous["results"] if r["variant"] == "fixed")
    campaign = store.get_campaign(row["campaign_id"])
    candidates = build_candidates(campaign["observed_requests"], campaign["observed_assets"])
    goal = campaign["config"]["goal"]
    rows = []
    async with PlanningContext(DATA_DIR) as context:
        refs = await context.context(campaign["project_id"], goal, "주문 stock 오류 재시도")
        for repeat in range(repeats):
            for mode in (("one_pass", "validated") if repeat % 2 == 0 else ("validated", "one_pass")):
                planner = OllamaPlanner()
                planner.instructions = refs["prompt"] if mode == "validated" else ""
                started = time.perf_counter()
                attempts, plan, usage = [], [], {"calls": 0, "input_tokens": 0, "output_tokens": 0}
                for _ in range(2 if mode == "validated" else 1):
                    plan, used = await planner.plan(goal, candidates, refs["references"], 11)
                    for key in usage: usage[key] += used.get(key, 0)
                    check = await context.call("check_plan_coverage", {"candidates": candidates, "scenarios": plan, "limit": 11})
                    attempts.append({"coverage": check, "plan": plan})
                    if not check["missing"]: break
                    planner.instructions = refs["prompt"] + " 누락 유형과 대표 변경 요청 경로를 보완하세요(같은 유형에서 POST/PUT/PATCH/DELETE 후보가 있으면 포함): " + ", ".join(check["missing"])
                final = complete_plan(candidates, plan, 11) if mode == "validated" and check["missing"] else plan
                record = {"repeat": repeat+1, "mode": mode, "attempts": attempts, "usage": usage,
                          "ms": round((time.perf_counter()-started)*1000, 2), "final_plan": final,
                          "coverage": coverage(candidates, final, 11), "repaired": final != plan}
                rows.append(record)
                output.write_text(json.dumps({"complete": False, "results": rows}, ensure_ascii=False, indent=2))
                print(json.dumps({"repeat": repeat+1, "mode": mode, "types": len(record["coverage"]["planned"]), "missing": record["coverage"]["missing"], "calls": usage["calls"]}), flush=True)
    result = {"complete": True, "algorithm_revision": "fault types + representative mutation scope", "model": OLLAMA_MODEL, "source_campaign": campaign["id"], "goal": goal,
              "candidates": candidates, "references": refs["references"], "repeats": repeats, "results": rows,
              "scope": "Identical observed requests/references/goal/model, alternating paired order. One-pass legacy prompt vs MCP prompt+bounded replan+coverage repair. Planning coverage, not browser defect recall; combined intervention does not isolate each component."}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("docs/benchmark-planning-20261021.json"))
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10: parser.error("repeats must be 1..10")
    asyncio.run(benchmark(args.repeats, args.output))
