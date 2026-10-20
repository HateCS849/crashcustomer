"""Black-box evaluation: normal negative control + separately injected defects.

Run the application first. The answer key below belongs only to the evaluator;
the actor receives the goal, contract, rendered observations and its action history.
"""
import argparse
import asyncio
import json
from pathlib import Path

import httpx

EXPECTED = {"healthy": set(), "reset": {"input_loss"}, "locked": {"retry_locked"}, "false_success": {"false_success"}}


async def evaluate(base: str, backend: str, repeats: int, output: Path):
    rows = []
    async with httpx.AsyncClient(base_url=base, timeout=15, trust_env=False) as client:
        for repeat in range(repeats):
            for fault, expected in EXPECTED.items():
                response = await client.post("/api/runs", json={"fault": fault, "backend": backend})
                response.raise_for_status()
                run_id = response.json()["id"]
                for _ in range(620):
                    response = await client.get(f"/api/runs/{run_id}")
                    response.raise_for_status()
                    run = response.json()
                    if run["status"] not in {"queued", "running", "replaying"}:
                        break
                    await asyncio.sleep(.5)
                else:
                    raise TimeoutError(f"Run did not finish: {run_id}")
                found = {f["code"] for f in run["findings"]}
                verified = {f["code"] for f in run["findings"] if f["verification"] == "confirmed"}
                passed = run["status"] == "completed" and found == expected and verified == expected
                if fault == "healthy":
                    passed = passed and run["result"] == "goal_completed"
                row = {"repeat": repeat + 1, "fault": fault, "run_id": run_id, "passed": passed,
                       "expected": sorted(expected), "found": sorted(found), "verified": sorted(verified),
                       "status": run["status"], "error": run["error"], "seconds": run.get("elapsed_seconds"),
                       "actions": len(run["actions"])}
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    expected_total = sum(len(EXPECTED[row["fault"]]) for row in rows)
    detected = sum(len(set(row["found"]) & EXPECTED[row["fault"]]) for row in rows)
    confirmed = sum(len(row["verified"]) for row in rows)
    finding_total = sum(len(row["found"]) for row in rows)
    summary = {"backend": backend, "runs": len(rows), "passed": sum(row["passed"] for row in rows),
               "defect_recall": detected / expected_total if expected_total else None,
               "reproduction_rate": confirmed / finding_total if finding_total else None,
               "false_positives": sum(len(set(row["found"]) - EXPECTED[row["fault"]]) for row in rows),
               "execution_errors": sum(row["status"] != "completed" for row in rows), "results": rows,
               "scope": "Controlled synthetic form, fixed transient-failure scenario; not real-world generalization."}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False), flush=True)
    return all(row["passed"] for row in rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8710")
    parser.add_argument("--backend", choices=["rule", "ollama"], default="rule")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("data/evaluation.json"))
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    raise SystemExit(0 if asyncio.run(evaluate(args.base_url, args.backend, args.repeats, args.output)) else 1)
