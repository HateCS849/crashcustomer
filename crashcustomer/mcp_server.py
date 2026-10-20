"""MCP server (stdio) exposing CrashCustomer to MCP clients such as Claude Code or Claude Desktop.

Thin client over the running HTTP API, so the dashboard and MCP share one store and one queue.
Register:  claude mcp add crashcustomer -- /path/to/.venv/bin/python -m crashcustomer.mcp_server
"""
import os
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP

API = os.getenv("CRASHCUSTOMER_API", "http://127.0.0.1:8710").rstrip("/")
mcp = FastMCP("crashcustomer")


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, timeout=30, trust_env=False)


async def call(method: str, path: str, **kwargs):
    async with client() as http:
        response = await http.request(method, path, **kwargs)
    if response.status_code >= 400:
        detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else response.text
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    return response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text


@mcp.tool()
async def list_projects() -> list[dict]:
    """등록된 프로젝트와 실행 가능 여부(execution.status: ready/blocked)를 조회합니다."""
    projects = await call("GET", "/api/projects")
    return [{k: p.get(k) for k in ("id", "name", "browser_url", "attachment_count", "execution")} for p in projects]


@mcp.tool()
async def check_connection(project_id: str) -> dict:
    """프로젝트 대상 주소가 허용된 테스트 호스트인지, 앱이 응답하는지 확인합니다."""
    return await call("GET", f"/api/projects/{project_id}/connection")


@mcp.tool()
async def start_campaign(project_id: str, goal: str = "화면의 주요 입력 양식을 작성해 제출하고 결과를 확인해줘.",
                         backend: Literal["rule", "ollama"] = "rule", max_scenarios: int = 11,
                         api_checks: bool = True, load_test: bool = True, load_max_users: int = 20) -> dict:
    """등록 앱에 배포 전 장애 사전 점검을 시작합니다. 기준 실행 후 관찰된 API·화면에 503/500/401/429, 연결 끊김, 지연,
    응답 형식 변경, 연속 실패, 중복 클릭, 느린 네트워크, 정적 파일 실패를 주입하고, API 입력 검증과 단계별 부하를 측정합니다."""
    return await call("POST", f"/api/projects/{project_id}/campaigns",
                      json={"goal": goal, "backend": backend, "max_scenarios": max_scenarios,
                            "api_checks": api_checks, "load_test": load_test, "load_max_users": load_max_users})


@mcp.tool()
async def get_campaign(campaign_id: str) -> dict:
    """사전 점검 진행 상태, 시나리오별 결함과 요약을 조회합니다."""
    campaign = await call("GET", f"/api/campaigns/{campaign_id}")
    return {k: campaign.get(k) for k in ("id", "status", "stage", "summary", "error", "scenarios", "probes", "references", "events")}


@mcp.tool()
async def get_campaign_report(campaign_id: str) -> str:
    """사전 점검 결과를 Markdown 보고서로 받습니다."""
    return await call("GET", f"/api/campaigns/{campaign_id}/report")


@mcp.tool()
async def start_demo_run(fault: Literal["healthy", "reset", "locked", "false_success"] = "reset",
                         backend: Literal["rule", "ollama"] = "rule") -> dict:
    """내장 행사 신청 데모에서 브라우저 테스트를 실행합니다."""
    return await call("POST", "/api/runs", json={"fault": fault, "backend": backend})


@mcp.tool()
async def get_run(run_id: str) -> dict:
    """브라우저 실행 하나의 상태, 결함, 재현 여부를 조회합니다."""
    run = await call("GET", f"/api/runs/{run_id}")
    return {k: run.get(k) for k in ("id", "status", "result", "label", "error", "elapsed_seconds", "usage")} | {
        "findings": [{k: f.get(k) for k in ("code", "title", "severity", "verification", "actual")} for f in run["findings"]]}


@mcp.tool()
async def start_lab_experiment(users: int = 10, pool_size: int = 10, external_delay_ms: int = 10000,
                               external_timeout_ms: int = 2500) -> dict:
    """로컬 실험실에서 커넥션 풀 장애 전파 실험(개선 전/후)을 시작합니다."""
    return await call("POST", "/api/lab/runs", json={"users": users, "pool_size": pool_size,
                                                     "external_delay_ms": external_delay_ms, "external_timeout_ms": external_timeout_ms})


@mcp.tool()
async def get_lab_report(experiment_id: str) -> str:
    """실험실 결과를 Markdown 보고서로 받습니다."""
    return await call("GET", f"/api/lab/runs/{experiment_id}/report")


if __name__ == "__main__":
    mcp.run()
