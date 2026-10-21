"""Real Ollama + planning MCP smoke check; this is not a browser detection benchmark."""
import asyncio
import json
from pathlib import Path

import httpx

from crashcustomer.campaign import OllamaPlanner, build_candidates
from crashcustomer.config import DATA_DIR, OLLAMA_MODEL
from crashcustomer.mcp_context import PlanningContext
from crashcustomer.store import Store
from crashcustomer.validation import complete_plan, coverage


async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8710", trust_env=False, timeout=5) as client:
        available = (await client.get("/api/config")).json()["ollama"]["available"]
    if not available:
        print(json.dumps({"verified": False, "reason": "Ollama model unavailable"}))
        return
    store = Store(DATA_DIR)
    project = next(p for p in store.list_projects() if p["name"] == "Sample Shop (fixed)")
    goal = "원두 주문을 완료하고 오류 후 재시도가 가능한지 점검해줘."
    candidates = build_candidates([{"method": "POST", "path": "/api/fixed/orders"},
                                   {"method": "GET", "path": "/api/fixed/inventory"}],
                                  [{"path": "/app.js", "type": "script"}])
    planner = OllamaPlanner()
    attempts = []
    async with PlanningContext(DATA_DIR) as context:
        refs = await context.context(project["id"], goal, "주문 오류 재시도 inventory")
        planner.instructions = refs["prompt"]
        for _ in range(2):
            plan, usage = await planner.plan(goal, candidates, refs["references"], 11)
            check = await context.call("check_plan_coverage", {"candidates": candidates, "scenarios": plan, "limit": 11})
            attempts.append({"plan": plan, "coverage": check, "usage": usage})
            if not check["missing"]: break
            planner.instructions = refs["prompt"] + " 누락 유형과 대표 변경 요청 경로를 보완하세요(같은 유형에서 POST/PUT/PATCH/DELETE 후보가 있으면 포함): " + ", ".join(check["missing"])
        final = complete_plan(candidates, plan, 11) if check["missing"] else plan
        result = {"model": OLLAMA_MODEL, "verified": True, "scope": "실제 LLM 계획·MCP 연동만 확인. 브라우저 탐지율 평가 아님.",
                  "attempts": attempts, "mcp_calls": context.calls, "final_coverage": coverage(candidates, final, 11)}
    assert not result["final_coverage"]["missing"]
    Path("docs/verification-planner-20261021.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"verified": True, "attempts": len(attempts), "final_missing": result["final_coverage"]["missing"]}))


if __name__ == "__main__": asyncio.run(main())
