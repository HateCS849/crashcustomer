"""Verify synthetic delivery and a real AI campaign trace. Never enables tracing or handles keys."""
import asyncio
import argparse
import json
from pathlib import Path

import httpx

from crashcustomer.config import DATA_DIR
from crashcustomer.tracing import tracing_enabled

OUTPUT = Path("docs/verification-langsmith-campaign-20261021.json")


async def verify(allow_external=False):
    if not allow_external:
        result = {"status": "approval_required", "synthetic_only": True,
                  "configured": tracing_enabled(),
                  "message": "외부 전송은 수행하지 않았습니다. 예제 앱의 합성 관찰·문서·모델 출력 전송 승인 후 --allow-synthetic-external-trace로 실행하세요."}
    elif not tracing_enabled():
        result = {"status": "not_configured", "synthetic_only": True,
                  "message": "LANGSMITH_TRACING=true, LANGSMITH_API_KEY, LANGSMITH_PROJECT 설정 후 서버를 재시작하세요. 실제 외부 전송·수신은 검증하지 않았습니다."}
    else:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8710", timeout=30, trust_env=False) as client:
            server = (await client.get("/api/tracing/status")).json()
            if not server["enabled"]:
                raise RuntimeError("새 설정을 반영하도록 CrashCustomer 서버를 먼저 재시작해주세요.")
            delivery = (await client.post("/api/tracing/verify", timeout=90)).json()
            if delivery["status"] != "verified":
                result = {"status": "synthetic_delivery_failed", "synthetic_delivery": delivery}
            else:
                projects = (await client.get("/api/projects")).json()
                p = next(p for p in projects if p["name"] == "Sample Shop (fixed)")
                response = await client.post(f"/api/projects/{p['id']}/campaigns", json={
                    "goal": "원두 정기배송 주문 양식을 작성해 주문을 완료해줘.", "backend": "ollama",
                    "max_scenarios": 1, "api_checks": False, "load_test": False})
                response.raise_for_status()
                campaign_id = response.json()["id"]
                async with client.stream("GET", f"/api/events/campaigns/{campaign_id}", timeout=600) as stream:
                    stream.raise_for_status()
                    async for _ in stream.aiter_lines(): pass
                campaign = (await client.get(f"/api/campaigns/{campaign_id}")).json()
                for _ in range(5):
                    response = await client.post(f"/api/tracing/campaigns/{campaign_id}/verify", timeout=90)
                    response.raise_for_status()
                    trace = response.json()
                    if trace["status"] in {"verified", "failed", "not_configured"}: break
                    await asyncio.sleep(2)
                result = {"status": trace["status"], "synthetic_only": True, "synthetic_delivery": delivery,
                          "campaign_id": campaign_id, "campaign_status": campaign["status"],
                          "baseline_passed": campaign.get("gate", {}).get("passed"), "campaign_trace": trace}
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return result["status"] == "verified"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-synthetic-external-trace", action="store_true",
                        help="Use only after explicit approval to transmit the sample app's synthetic trace data.")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.allow_synthetic_external_trace)) else 2)
