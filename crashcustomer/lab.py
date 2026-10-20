"""Local experimental controller. HTTP timings and SQL resource metrics are measured, not drawn."""
import asyncio
import json
import math
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from .config import BASE_URL, OLLAMA_MODEL, OLLAMA_URL
from .lab_service import LabRuntime
from .store import now
from .tracing import graph_config, traced


class LabRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    users: int = Field(default=10, ge=1, le=200)
    pool_size: int = Field(default=10, ge=1, le=30)
    external_delay_ms: int = Field(default=10000, ge=0, le=15000)
    external_timeout_ms: int = Field(default=2500, ge=50, le=5000)
    pool_acquire_ms: int = Field(default=500, ge=50, le=2000)
    login_slo_ms: int = Field(default=3000, ge=100, le=20000)
    log_queue_capacity: int = Field(default=4, ge=1, le=200)
    log_write_ms: int = Field(default=50, ge=0, le=300)
    external_result: Literal["success", "error"] = "success"
    comparison: Literal["both", "before", "after"] = "both"
    analysis_backend: Literal["metrics", "ollama"] = "metrics"


def percentile(values, fraction):
    return round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 2) if values else None


def summarize_case(case, config):
    endpoints = {}
    for name in ("login", "contents", "search"):
        rows = [r for r in case["requests"] if r["endpoint"] == name]
        successful = sum(200 <= r["status"] < 300 for r in rows)
        durations = [r["duration_ms"] for r in rows]
        endpoints[name] = {"total": len(rows), "successful": successful, "failed": len(rows) - successful,
                           "p95_ms": percentile(durations, .95), "max_ms": round(max(durations), 2) if durations else None,
                           "reasons": {reason: sum(r.get("reason") == reason for r in rows) for reason in sorted({r.get("reason", "ok") for r in rows})}}
    observers = [row for row in case["requests"] if row["endpoint"] in {"contents", "search"}]
    case["summary"] = {"endpoints": endpoints, "protected_reads_passed": len(observers) == 2 and all(200 <= r["status"] < 300 for r in observers),
                       "login_slo_passed": bool(endpoints["login"]["total"]) and endpoints["login"]["max_ms"] <= config["login_slo_ms"],
                       "transport_errors": sum(r["status"] == 0 for r in case["requests"])}
    return case["summary"]


class LabManager:
    def __init__(self, store_provider, client_factory=None):
        self.store_provider = store_provider
        self.client_factory = client_factory
        self.sessions = {}
        self.tasks = {}
        self.semaphore = asyncio.Semaphore(1)

    @property
    def store(self):
        return self.store_provider()

    def emit(self, run, stage, message):
        run["stage"] = stage
        run["events"].append({"time": now(), "stage": stage, "message": message})
        self.store.save_experiment(run)

    def client(self, config):
        if self.client_factory:
            return self.client_factory()
        return httpx.AsyncClient(base_url=BASE_URL, trust_env=False,
                                 limits=httpx.Limits(max_connections=config["users"] + 10, max_keepalive_connections=config["users"] + 10),
                                 timeout=httpx.Timeout(25, connect=3, pool=3))

    async def start(self, config: LabRequest):
        if len(self.tasks) >= 2:
            raise ValueError("한 번에 실험 1개와 대기 1개까지 실행할 수 있습니다.")
        if not self.client_factory and urlsplit(BASE_URL).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("이 실험실은 로컬 주소에서만 실행합니다.")
        run = self.store.create_experiment(config.model_dump())

        async def work():
            try:
                async with self.semaphore:
                    await self.execute(run["id"])
            except asyncio.CancelledError:
                current = self.store.get_experiment(run["id"])
                if current and current["status"] in {"queued", "running"}:
                    current.update(status="cancelled", finished_at=now())
                    self.store.save_experiment(current)
                raise

        task = asyncio.create_task(work())
        self.tasks[run["id"]] = task
        task.add_done_callback(lambda _: self.tasks.pop(run["id"], None))
        return run

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def execute(self, run_id):
        run = self.store.get_experiment(run_id)
        started = time.perf_counter()
        run["status"] = "running"
        self.emit(run, "plan", "같은 입력으로 버전별 실험을 준비합니다. 로그인 부하 외에 목록·검색 관측 요청이 각각 1개 추가됩니다.")
        try:
            async with asyncio.timeout(90):
                graph = StateGraph(dict)

                async def plan(state):
                    run["plan"] = {"load_users": run["config"]["users"], "observer_requests": 2,
                                   "variants": ["before", "after"] if run["config"]["comparison"] == "both" else [run["config"]["comparison"]],
                                   "engine": "real HTTP + SQLite connection pool", "model": "구조 재현용 Python 모델. Hangari/Spring/Hikari 실행 아님."}
                    self.store.save_experiment(run)
                    return state

                async def execute_cases(state):
                    for variant in run["plan"]["variants"]:
                        await self.run_case(run, variant)
                    return state

                async def analyze(state):
                    self.emit(run, "analyze", "측정값으로 영향 범위와 로그 보존 여부를 검사합니다.")
                    run["findings"] = build_findings(run)
                    if run["config"]["analysis_backend"] == "ollama":
                        try:
                            run["ai_analysis"] = await ai_analysis(run)
                        except Exception as error:
                            run["ai_error"] = f"AI 해석을 완료하지 못했습니다: {type(error).__name__}. 실측 결과는 보존됩니다."
                    return state

                graph.add_node("plan", plan)
                graph.add_node("experiment", execute_cases)
                graph.add_node("analyze", analyze)
                graph.add_edge(START, "plan")
                graph.add_edge("plan", "experiment")
                graph.add_edge("experiment", "analyze")
                graph.add_edge("analyze", END)
                await graph.compile().ainvoke({}, graph_config("crashcustomer.lab", {**run, "backend": run["config"]["analysis_backend"]}))
            run["status"] = "completed"
            self.emit(run, "complete", "실험을 완료했습니다. 수치는 이번 로컬 실행의 측정 결과입니다.")
        except asyncio.CancelledError:
            run["status"] = "cancelled"
            self.emit(run, "cancelled", "실험을 중단하고 로컬 요청과 DB 자원을 정리했습니다.")
            raise
        except Exception as error:
            run.update(status="error", error=f"{type(error).__name__}: {str(error)[:1200]}")
            self.emit(run, "error", "실험 실행 오류입니다. 서비스의 측정된 결함과 구분합니다.")
        finally:
            run.update(finished_at=now(), elapsed_seconds=round(time.perf_counter() - started, 2))
            self.store.save_experiment(run)

    async def run_case(self, run, variant):
        config = run["config"]
        token = uuid.uuid4().hex
        directory = self.store.artifacts / "lab" / run["id"] / variant
        case = {"variant": variant, "status": "running", "requests": [], "samples": [], "final": None, "summary": None}
        run["cases"].append(case)
        started = time.perf_counter()
        self.emit(run, variant, ("개선 전" if variant == "before" else "개선 후") + " · 새 DB와 커넥션 풀에서 HTTP 요청을 발생시킵니다.")
        async with self.client(config) as outer, self.client(config) as dependency:
            runtime = LabRuntime(config, variant, directory, dependency)
            self.sessions[token] = runtime
            await runtime.logs.start()
            stop_monitor = asyncio.Event()

            async def monitor():
                while not stop_monitor.is_set():
                    case["samples"].append({"elapsed_ms": round((time.perf_counter() - started) * 1000), **runtime.snapshot()})
                    if len(case["samples"]) % 4 == 0:
                        self.store.save_experiment(run)
                    try:
                        await asyncio.wait_for(stop_monitor.wait(), .1)
                    except TimeoutError:
                        pass

            async def send(endpoint, user):
                dispatched = time.perf_counter()
                record = {"user": user, "endpoint": endpoint, "started_ms": round((dispatched - started) * 1000, 2)}
                try:
                    response = await outer.request("POST" if endpoint == "login" else "GET", f"/lab/service/{token}/{endpoint}", params={"user": user})
                    payload = response.json()
                    record.update(status=response.status_code, reason=payload.get("reason", "ok"))
                except httpx.HTTPError as error:
                    record.update(status=0, reason="generator_transport_error", error=type(error).__name__)
                finally:
                    record["duration_ms"] = round((time.perf_counter() - dispatched) * 1000, 2)
                    case["requests"].append(record)

            monitoring = asyncio.create_task(monitor())
            workers = [asyncio.create_task(send("login", f"login-{index+1:03}")) for index in range(config["users"])]
            try:
                try:
                    await asyncio.wait_for(runtime.dependency_ready.wait(), 2)
                except TimeoutError:
                    pass
                workers += [asyncio.create_task(send(endpoint, "observer-" + endpoint)) for endpoint in ("contents", "search")]
                await asyncio.gather(*workers)
                case["load_duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
                await runtime.logs.stop()
                runtime.logs.worker = None
                case["final"] = runtime.snapshot()
                case["samples"].append({"elapsed_ms": round((time.perf_counter() - started) * 1000), **runtime.snapshot()})
                case["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
                case["status"] = "completed"
                summarize_case(case, config)
                self.store.save_experiment(run)
            finally:
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                stop_monitor.set()
                await monitoring
                await runtime.close()
                self.sessions.pop(token, None)


def build_findings(run):
    findings = []
    for case in run["cases"]:
        if not case.get("summary"):
            continue
        label = "개선 전" if case["variant"] == "before" else "개선 후"
        summary, final = case["summary"], case["final"]
        if not summary["protected_reads_passed"]:
            findings.append({"severity": "failure", "variant": case["variant"], "title": label + " · 목록·검색 영향 관찰",
                             "detail": "독립된 목록·검색 관측 요청 중 실패가 발생했습니다. 요청별 응답과 커넥션 점유 시계열을 함께 확인하세요."})
        else:
            findings.append({"severity": "pass", "variant": case["variant"], "title": label + " · 목록·검색 정상 응답",
                             "detail": "관측 요청 2개가 모두 성공했습니다. 다른 경로나 반복 사용 전체를 검증한 결과는 아닙니다."})
        if not summary["login_slo_passed"]:
            findings.append({"severity": "failure", "variant": case["variant"], "title": label + " · 로그인 응답 목표 초과",
                             "detail": f"최대 {summary['endpoints']['login']['max_ms']}ms, 목표 {run['config']['login_slo_ms']}ms입니다."})
        if final["pool"]["acquisition_timeouts"]:
            findings.append({"severity": "warning", "variant": case["variant"], "title": label + " · DB 커넥션 획득 실패",
                             "detail": f"획득 대기 초과 {final['pool']['acquisition_timeouts']}회. 로그인·조회·오류 로그가 같은 풀을 사용합니다."})
        if final["logs"]["file_saved"]:
            findings.append({"severity": "info", "variant": case["variant"], "title": label + " · 파일 로그 보존",
                             "detail": f"DB 기록 {final['logs']['db_saved']}건, 파일 기록 {final['logs']['file_saved']}건입니다."})
        if final["logs"]["received"] != final["logs"]["db_saved"] + final["logs"]["file_saved"]:
            findings.append({"severity": "warning", "variant": case["variant"], "title": label + " · DB 오류 로그 기록 실패",
                             "detail": f"기록 요청 {final['logs']['received']}건 중 DB {final['logs']['db_saved']}건·파일 {final['logs']['file_saved']}건을 보존했습니다."})
    return findings


class AIInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=800)
    hypotheses: list[str] = Field(max_length=4)
    next_experiments: list[str] = Field(max_length=3)


@traced("lab.ai_analysis", run_type="llm")
async def ai_analysis(run):
    evidence = {"config": run["config"], "cases": [{"variant": c["variant"], "summary": c["summary"], "resources": c["final"]} for c in run["cases"]]}
    async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
        response = await client.post(OLLAMA_URL + "/api/chat", json={
            "model": OLLAMA_MODEL, "stream": False, "think": False, "format": AIInterpretation.model_json_schema(),
            "options": {"temperature": 0, "num_predict": 800},
            "messages": [{"role": "system", "content": "실측 데이터만 근거로 한국어 실험 해석을 작성하세요. 환경은 Python/SQLite 구조 재현 모델이며 실제 Hangari/Spring 실험이 아닙니다. summary는 3문장 이내, hypotheses는 원인 가설, next_experiments는 검증할 추가 실험입니다. 측정에 없는 수치나 과거 운영 사실을 만들지 마세요. 전후 변경이 묶여 있어 각 변경의 단독 효과를 확정할 수 없습니다."},
                         {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]})
        response.raise_for_status()
    return AIInterpretation.model_validate_json(response.json()["message"]["content"]).model_dump()


def lab_report(run):
    config = run["config"]
    lines = ["# CrashCustomer 로컬 장애 전파 실험", "", "실제 HTTP와 SQLite 커넥션 풀을 사용한 구조 재현 결과입니다. Hangari/Spring/Hikari 코드의 성능 또는 과거 운영 사실을 검증한 결과가 아닙니다.", "",
             f"- 실행: {run['id']}", f"- 상태: {run['status']}", f"- 로그인 부하 사용자: {config['users']}명 + 목록·검색 관측 요청 각각 1개",
             f"- 풀 크기: {config['pool_size']}", f"- 외부 응답 지연: {config['external_delay_ms']}ms",
             f"- 개선 후 외부 호출 제한: {config['external_timeout_ms']}ms", f"- 로그인 응답 목표: {config['login_slo_ms']}ms", "",
             "| 버전 | API | 요청 | 성공 | 실패 | P95(ms) | 최대(ms) |", "|---|---|---:|---:|---:|---:|---:|"]
    for case in run["cases"]:
        if not case.get("summary"):
            continue
        for endpoint, value in case["summary"]["endpoints"].items():
            lines.append(f"| {case['variant']} | {endpoint} | {value['total']} | {value['successful']} | {value['failed']} | {value['p95_ms']} | {value['max_ms']} |")
        logs, pool = case["final"]["logs"], case["final"]["pool"]
        lines += ["", f"{case['variant']}: 최대 커넥션 점유 {pool['peak_active']}/{pool['size']}, 획득 대기 초과 {pool['acquisition_timeouts']}회. 로그 DB {logs['db_saved']}건 / 파일 {logs['file_saved']}건 / 기록 실패 {logs['failed']}건.", ""]
    lines += ["## 측정 기반 판정", ""]
    for finding in run["findings"]:
        lines += ["- " + finding["title"] + ": " + finding["detail"]]
    if run.get("ai_analysis"):
        lines += ["", "## AI 해석 (가설)", "", run["ai_analysis"]["summary"]]
        lines += ["- " + text for text in run["ai_analysis"]["hypotheses"]]
    lines += ["", "## 실험 범위", "", "로그인 요청은 사용자별 1회이며 목록·검색 관측 요청은 각각 1회입니다. 사용자 행동 반복·브라우저 렌더링·실제 OAuth 인증·분산 서버·운영 DB 성능은 검증하지 않았습니다. 실험마다 새 DB와 자원을 사용합니다. 여러 개선을 함께 적용하므로 개별 변경의 효과는 별도 실험이 필요합니다."]
    return "\n".join(lines) + "\n"
