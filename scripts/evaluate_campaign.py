"""Black-box evaluation of the pre-deployment campaign on the sample shop (examples/shop_app.py).

Start CrashCustomer and the sample shop first. The answer key below belongs only to the
evaluator; the planner and explorer see the rendered app, observed requests and attachments.
"""
import argparse
import asyncio
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {"buggy": {"input_loss", "double_submit", "unhandled_error", "silent_failure", "auth_expiry_unhandled",
                     "broken_render", "retry_storm", "duplicate_processing", "asset_failure_silent",
                     "input_validation_500", "load_slo_breach"},
            "fixed": set()}
GOAL = "원두 정기배송 주문 양식을 작성해 주문을 완료해줘."


async def ensure_project(client, shop: str, variant: str) -> str:
    name = f"Sample Shop ({variant})"
    for project in (await client.get("/api/projects")).json():
        if project["name"] == name and project["browser_url"] == f"{shop}/{variant}":
            return project["id"]
    project = (await client.post("/api/projects", json={"name": name, "browser_url": f"{shop}/{variant}"})).json()
    for filename in ("shop-openapi.yaml", "shop-runbook.md"):
        content = (ROOT / "examples" / filename).read_bytes()
        (await client.post(f"/api/projects/{project['id']}/attachments", files={"file": (filename, content)})).raise_for_status()
    return project["id"]


async def evaluate(base: str, shop: str, backend: str, scenarios: int, output: Path, concurrency: int = 1):
    rows = []
    async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as client:
        for variant, expected in EXPECTED.items():
            project_id = await ensure_project(client, shop, variant)
            response = await client.post(f"/api/projects/{project_id}/campaigns", json={"goal": GOAL, "backend": backend, "max_scenarios": scenarios, "scenario_concurrency": concurrency})
            response.raise_for_status()
            campaign_id = response.json()["id"]
            for _ in range(600):
                campaign = (await client.get(f"/api/campaigns/{campaign_id}")).json()
                if campaign["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(1)
            confirmed = {f["code"] for s in campaign["scenarios"] for f in s.get("findings", []) if f["verification"] == "confirmed"}
            confirmed |= {f["code"] for f in (campaign.get("probes") or {}).get("findings", [])}
            row = {"variant": variant, "campaign_id": campaign_id, "status": campaign["status"], "error": campaign["error"],
                   "expected": sorted(expected), "confirmed": sorted(confirmed), "missed": sorted(expected - confirmed),
                   "false_positives": sorted(confirmed - expected),
                   "by_scenario": {s["label"]: sorted({f["code"] for f in s.get("findings", []) if f["verification"] == "confirmed"}) for s in campaign["scenarios"]},
                   "summary": campaign["summary"], "timings": campaign.get("timings"), "elapsed_seconds": campaign.get("elapsed_seconds")}
            rows.append(row)
            print(json.dumps({k: row[k] for k in ("variant", "status", "confirmed", "missed", "false_positives")}, ensure_ascii=False), flush=True)
    expected_total = sum(len(EXPECTED[r["variant"]]) for r in rows)
    result = {"backend": backend, "max_scenarios": scenarios, "scenario_concurrency": concurrency,
              "recall": sum(len(set(r["confirmed"]) & EXPECTED[r["variant"]]) for r in rows) / expected_total,
              "false_positives": sum(len(r["false_positives"]) for r in rows),
              "llm_calls": sum(r["summary"]["llm_usage"]["calls"] for r in rows if r["summary"]),
              "elapsed_seconds": round(sum(r["summary"]["elapsed_seconds"] for r in rows if r["summary"]), 2), "results": rows,
              "scope": "Sample shop with planted defects; one campaign per variant. Not real-world generalization."}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, ensure_ascii=False))
    return result["false_positives"] == 0 and result["recall"] == 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8710")
    parser.add_argument("--shop-url", default="http://127.0.0.1:8720")
    parser.add_argument("--backend", choices=["rule", "ollama"], default="rule")
    parser.add_argument("--scenarios", type=int, default=11)
    parser.add_argument("--concurrency", type=int, default=1, help="scenarios run at the same time (server BROWSER_SLOTS also caps this)")
    parser.add_argument("--output", type=Path, default=Path("data/evaluation-campaign.json"))
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(evaluate(args.base_url, args.shop_url, args.backend, args.scenarios, args.output, args.concurrency)) else 1)
