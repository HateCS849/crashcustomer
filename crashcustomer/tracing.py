"""Optional LangSmith tracing. Disabled unless LANGSMITH_TRACING=true and an API key are set.

LangGraph graphs are traced automatically through LangChain callbacks when enabled; direct
Ollama HTTP calls are wrapped with `traced` so prompts, outputs and token counts appear in the
same trace tree. When disabled, `traceable` passes calls through without sending data.
"""
import os
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from langsmith import Client, traceable
from langsmith.utils import LangSmithNotFoundError
from .secrets import scrub


def tracing_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").lower() == "true" and bool(os.getenv("LANGSMITH_API_KEY"))


def tracing_status() -> dict:
    from .config import DATA_DIR
    path = DATA_DIR / "tracing-verification.json"
    try:
        evidence = json.loads(path.read_text()) if path.exists() else None
    except (OSError, ValueError):
        evidence = {"status": "unreadable", "message": "이전 검증 기록을 읽지 못했습니다. 다시 검증해주세요."}
    return {"enabled": tracing_enabled(), "project": os.getenv("LANGSMITH_PROJECT", "default"),
            "delivery": evidence, "message": "활성 설정과 실제 수신 검증은 별개입니다. 합성 추적 검증은 프로젝트 데이터 없이 실행됩니다."}


def traced(name: str, run_type: str = "chain"):
    return traceable(name=name, run_type=run_type, process_inputs=scrub, process_outputs=scrub)


def graph_config(name: str, run: dict, **extra) -> dict:
    """LangGraph invocation config: names the trace and tags it with the run for filtering."""
    return {"run_name": name, "tags": ["crashcustomer", run.get("backend", "rule")],
            "metadata": {"run_id": run["id"], "backend": run.get("backend"), **extra}}


READBACK_WAITS = (0, 1, 2, 3, 4, 5)


def _verify(client_factory=Client, waits=READBACK_WAITS):
    run_id = uuid.uuid4()
    client = client_factory(auto_batch_tracing=False, timeout_ms=3000)
    try:
        client.create_run("crashcustomer.synthetic-delivery-check", {"synthetic": True}, "chain",
                          id=run_id, project_name=os.getenv("LANGSMITH_PROJECT", "default"),
                          start_time=datetime.now(timezone.utc), tags=["crashcustomer", "synthetic-verification"])
        client.update_run(run_id, outputs={"synthetic": True, "correlation_id": str(run_id)}, end_time=datetime.now(timezone.utc))
        # Successful writes alone are insufficient: read back and verify this specific output.
        # Ingestion is asynchronous, so the run and its outputs appear a few seconds later.
        received, verified = None, False
        for wait in waits:
            time.sleep(wait)
            try:
                received = client.read_run(run_id)
            except LangSmithNotFoundError:
                continue
            verified = str(received.id) == str(run_id) and (received.outputs or {}).get("correlation_id") == str(run_id)
            if verified:
                break
        if received is None:
            return {"status": "not_received", "run_id": str(run_id), "synthetic_only": True}
        return {"status": "verified" if verified else "not_verified", "run_id": str(run_id),
                "url": getattr(received, "url", None) if verified else None, "synthetic_only": True}
    finally:
        client.close()


async def verify_delivery(directory: Path, client_factory=Client, waits=READBACK_WAITS):
    if not tracing_enabled():
        result = {"status": "not_configured", "message": "LANGSMITH_TRACING=true 및 API 키 설정이 필요합니다. 실제 전송은 수행하지 않았습니다.", "synthetic_only": True}
    else:
        try:
            result = await asyncio.to_thread(_verify, client_factory, waits)
        except Exception as error:
            result = {"status": "failed", "error_type": type(error).__name__, "synthetic_only": True}
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "tracing-verification.json"
    temp = destination.with_name("tracing-" + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temp.replace(destination)
    return result


def _campaign_evidence(campaign, client_factory=Client):
    """Read back an actual graph trace, including LLM and MCP child spans."""
    client = client_factory(auto_batch_tracing=False, timeout_ms=3000)
    try:
        query = f'and(eq(name,"crashcustomer.campaign"),eq(metadata_key,"run_id"),eq(metadata_value,{json.dumps(campaign["id"])}))'
        roots = list(client.list_runs(project_name=os.getenv("LANGSMITH_PROJECT", "default"),
                                      is_root=True, filter=query, limit=10))
        root = next((r for r in roots if (r.extra or {}).get("metadata", {}).get("run_id") == campaign["id"]), None)
        if root is None:
            return {"status": "not_received", "campaign_id": campaign["id"], "message": "해당 캠페인의 루트 추적이 아직 조회되지 않았습니다."}
        # The API caps one request at 100 runs; let the SDK page through the trace (bounded at 1000 spans).
        spans = list(islice(client.list_runs(project_name=os.getenv("LANGSMITH_PROJECT", "default"), trace_id=root.trace_id), 1000))
        names = sorted({s.name for s in spans})
        llm = [s for s in spans if s.run_type == "llm" and s.end_time and not s.error]
        mcp = [s for s in spans if s.name in {"mcp.context", "mcp.tool"} and s.end_time and not s.error]
        verified = bool(root.end_time and not root.error and llm and mcp)
        return {"status": "verified" if verified else "incomplete_trace", "campaign_id": campaign["id"],
                "root_id": str(root.id), "trace_id": str(root.trace_id), "url": getattr(root, "url", None),
                "root_finished": bool(root.end_time), "root_error": bool(root.error), "span_count": len(spans),
                "completed_llm_spans": len(llm), "completed_mcp_spans": len(mcp), "span_names": names,
                "scope": "실제 캠페인 그래프 및 완료된 LLM·MCP 하위 추적 수신. 프로젝트 데이터나 프롬프트 원문은 이 증거 파일에 저장하지 않습니다."}
    finally:
        client.close()


async def verify_campaign_delivery(directory: Path, campaign: dict, client_factory=Client):
    if not tracing_enabled():
        result = {"status": "not_configured", "campaign_id": campaign["id"],
                  "message": "LangSmith 설정이 없어 실제 캠페인 추적 수신을 조회하지 않았습니다."}
    else:
        try:
            result = await asyncio.to_thread(_campaign_evidence, campaign, client_factory)
        except Exception as error:
            result = {"status": "failed", "campaign_id": campaign["id"], "error_type": type(error).__name__}
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    destination = directory / "campaign-traces"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / (campaign["id"] + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
