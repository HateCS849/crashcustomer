"""Live regression against examples/auth_app.py; only synthetic credentials/data."""
import asyncio
import json
from pathlib import Path

import httpx


async def main():
    evidence = {"target": "http://127.0.0.1:8721/", "synthetic_only": True, "cases": []}
    token = "crashcustomer-synthetic-token"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8710", timeout=30, trust_env=False) as client:
        projects = (await client.get("/api/projects")).json()
        p = next((p for p in projects if p["name"] == "Synthetic Auth"), None)
        if p is None:
            response = await client.post("/api/projects", json={"name": "Synthetic Auth", "browser_url": evidence["target"],
                                                              "description": "합성 토큰 인증 전달·검증 회귀용"})
            response.raise_for_status()
            p = response.json()
        for authenticated in (False, True):
            response = await client.post(f"/api/projects/{p['id']}/campaigns", json={
                "goal": "이름을 입력하고 신청을 완료해줘.", "max_steps": 6, "max_scenarios": 1,
                "api_checks": False, "load_max_users": 2, "test_token": token if authenticated else None})
            response.raise_for_status()
            run_id = response.json()["id"]
            updates, child_updates, last_id, run = 0, 0, None, None
            async with client.stream("GET", f"/api/events/campaigns/{run_id}", timeout=120) as stream:
                stream.raise_for_status()
                async for line in stream.aiter_lines():
                    if line.startswith("id: "):
                        last_id = line[4:]
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        if "status" in data:
                            run = data
                            updates += 1
                            child_updates += bool(data.get("active_run", {}).get("events"))
            assert run is not None and run["finished_at"] and run["elapsed_seconds"] is not None
            assert run["status"] == ("partial" if authenticated else "inconclusive"), run
            if authenticated:
                assert run["gate"]["passed"] and run["probes"]["load"]["valid"], run
                assert run["probes"]["load"]["stages"][-1]["error_rate"] == 0
                assert run["mcp"]["calls"] and child_updates
                for child_id in run["run_ids"]:
                    child = (await client.get(f"/api/runs/{child_id}")).json()
                    assert token not in json.dumps(child)
                    assert not child.get("trace_enabled", True)
                    assert (await client.get(f"/artifacts/{child_id}/explore/trace.zip")).status_code == 404
            else:
                assert not run["gate"]["passed"] and run["probes"] is None
            assert token not in json.dumps(run)
            resumed = await client.get(f"/api/events/campaigns/{run_id}", headers={"Last-Event-ID": last_id})
            assert "event: update" not in resumed.text and "event: done" in resumed.text
            evidence["cases"].append({"authenticated": authenticated, "campaign_id": run_id,
                                      "status": run["status"], "validation": run["validation"],
                                      "sse_updates": updates, "worker_updates": child_updates,
                                      "resume_deduplicated": True, "token_not_saved": True,
                                      "load": run["probes"]["load"] if run["probes"] else None})
        evidence["langsmith"] = (await client.post("/api/tracing/verify")).json()
    path = Path("docs/verification-upgrade-20261021.json")
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": [{"authenticated": c["authenticated"], "status": c["status"], "sse_updates": c["sse_updates"]} for c in evidence["cases"]],
                      "langsmith": evidence["langsmith"]["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
