import asyncio
import time
from typing import TypedDict
from urllib.parse import urlsplit
import re

from langgraph.graph import END, START, StateGraph

from .browser import BrowserTools
from .checks import GENERIC_CONTRACT, check_contract, check_initial, check_resilience, generic_success, successful
from .config import BASE_URL
from .models import CONTRACT, Action
from .policy import OllamaPolicy, RulePolicy
from .store import Store, now
from .tracing import graph_config
from .secrets import scrub


class State(TypedDict, total=False):
    snapshot: dict
    previous: dict | None
    action: dict | None
    actions: list[dict]
    stop: bool
    result: str


def emit(store: Store, run: dict, node: str, message: str, **detail):
    run["events"].append({"time": now(), "node": node, "message": message, **detail})
    store.save_run(run)


class Target:
    """What one run tests: the built-in demo session or a registered app with injected faults."""
    def __init__(self, store: Store, run: dict, auth_headers=None):
        self.auth_headers = auth_headers or {}
        self.project = run.get("kind") == "project"
        self.store, self.run = store, run
        self.contract = GENERIC_CONTRACT if self.project else CONTRACT

    def check(self, previous, current, action):
        if not self.project:
            return check_contract(previous, current, action)
        return check_initial(current) if previous is None else check_resilience(previous, current, action)

    def succeeded(self, previous, current):
        return generic_success(previous, current) if self.project else successful(current)

    def open(self, phase: str) -> BrowserTools:
        directory = self.store.artifacts / self.run["id"] / phase
        if self.project:
            return BrowserTools(self.run["target_url"], directory, self.run["id"], phase, scope="origin", faults=self.run.get("faults"), auth_headers=self.auth_headers)
        # A new server-side session resets attempt counters and registrations as well as browser storage.
        token = self.store.create_session(self.run["fault"])
        if phase == "explore":
            self.run["target_url"] = f"/demo/{token}"
        return BrowserTools(f"{BASE_URL}/demo/{token}", directory, self.run["id"], phase)


def build_explorer(store: Store, run: dict, browser: BrowserTools, target: Target):
    policy = OllamaPolicy(target.contract) if run["backend"] == "ollama" else RulePolicy()

    async def observe(state: State):
        snapshot = await browser.observe(len(state["actions"]))
        run["observations"].append(snapshot)
        emit(store, run, "observe", "현재 화면과 네트워크 응답을 관찰했습니다.", step=snapshot["step"], screenshot=snapshot["screenshot"])
        return {"previous": state.get("snapshot"), "snapshot": snapshot}

    async def assess(state: State):
        found = target.check(state.get("previous"), state["snapshot"], state.get("action"))
        if found:
            run["findings"] = [{**f, "verification": "pending"} for f in found]
            emit(store, run, "assess", f"서비스 계약 위반 {len(found)}건을 발견했습니다.")
            return {"stop": True, "result": "findings_detected"}
        if target.succeeded(state.get("previous"), state["snapshot"]):
            accepted = any(r["method"] != "GET" and 200 <= r["status"] < 300 for r in browser.responses)
            return {"stop": True, "result": "goal_completed" if accepted else "inconclusive"}
        if len(state["actions"]) >= run["max_steps"]:
            emit(store, run, "assess", "행동 예산에 도달했습니다. 정상 판정 없이 탐색을 종료합니다.")
            return {"stop": True, "result": "budget_exhausted"}
        return {"stop": False}

    async def decide(state: State):
        action, usage = await policy.decide(run["goal"], state["snapshot"], state["actions"])
        for key, value in usage.items():
            run["usage"][key] += value
        emit(store, run, "decide", action.reason, action=action.model_dump())
        if action.kind == "finish":
            return {"stop": True, "result": "inconclusive"}
        return {"action": action.model_dump(), "stop": False}

    async def act(state: State):
        action = Action.model_validate(state["action"])
        await browser.act(action, state["snapshot"])
        actions = state["actions"] + [action.model_dump()]
        run["actions"] = actions
        emit(store, run, "act", f"{action.kind} · {action.target}", step=len(actions))
        return {"actions": actions}

    graph = StateGraph(State)
    graph.add_node("observe", observe)
    graph.add_node("assess", assess)
    graph.add_node("decide", decide)
    graph.add_node("act", act)
    graph.add_edge(START, "observe")
    graph.add_edge("observe", "assess")
    graph.add_conditional_edges("assess", lambda state: "end" if state["stop"] else "next", {"end": END, "next": "decide"})
    graph.add_conditional_edges("decide", lambda state: "end" if state["stop"] else "next", {"end": END, "next": "act"})
    graph.add_edge("act", "observe")
    return graph.compile()


async def replay(store: Store, run: dict, target: Target):
    run["status"] = "replaying"
    record = {"observations": [], "findings": [], "error": None,
              "trace": f"/artifacts/{run['id']}/replay/trace.zip", "server_state": "not_reset" if target.project else "reset"}
    run["replays"].append(record)
    emit(store, run, "replay", "새 브라우저에서 같은 장애 조건으로 동작을 다시 실행합니다. 등록 앱의 서버 데이터는 초기화하지 않습니다."
         if target.project else "새 브라우저와 초기화된 서버 세션에서 동작을 다시 실행합니다.")
    try:
        async with target.open("replay") as browser:
            previous = await browser.observe(0)
            record["observations"].append(previous)
            record["findings"].extend(target.check(None, previous, None))
            for step, raw in enumerate(run["actions"], 1):
                await browser.act(Action.model_validate(raw), previous)
                current = await browser.observe(step)
                record["observations"].append(current)
                record["findings"].extend(target.check(previous, current, raw))
                previous = current
    except Exception as error:
        record["error"] = str(error)[:1500]
    def signature(f):
        def path(url):
            value = urlsplit(url).path
            return value if target.project else re.sub(r"/api/demo/[^/]+/", "/api/demo/{session}/", value)
        injected = tuple(sorted({(i.get("method", ""), i.get("path", ""), i.get("mode", "")) for i in f.get("injected", [])}))
        network = tuple(sorted({(r.get("method", ""), path(r.get("url", "")), r.get("status", 0)) for r in f.get("network", [])}))
        return f["code"], injected, network
    reproduced = {signature(f) for f in record["findings"]}
    for finding in run["findings"]:
        finding["verification"] = "confirmed" if signature(finding) in reproduced else "unconfirmed"
        finding["replay_after"] = next((f["after"] for f in record["findings"] if signature(f) == signature(finding)), None)
        finding["verification_scope"] = "같은 결함 코드·API·장애 조건의 관찰 재현. 등록 앱의 서버 데이터는 초기화하지 않음." if target.project else "초기화된 데모 세션 재현"
    count = sum(f["verification"] == "confirmed" for f in run["findings"])
    emit(store, run, "replay", f"{len(run['findings'])}건 중 {count}건의 동일 증상을 독립 재현했습니다.")
    run["result"] = "findings_confirmed" if count == len(run["findings"]) else "findings_unconfirmed"


def report_markdown(run: dict) -> str:
    backend = "Ollama AI" if run["backend"] == "ollama" else "규칙 기반 브라우저 기준선 (AI 아님)"
    lines = ["# CrashCustomer 실행 보고서", "", f"- 실행: `{run['id']}`", f"- 모드: {backend}"]
    if run.get("kind") == "project":
        lines += [f"- 대상: {run['target_url']}", f"- 장애 시나리오: {run.get('label', '장애 주입 없음')}"]
    lines += [
             f"- 목표: {run['goal']}", f"- 상태: {run['status']} / {run.get('result')}",
             f"- 탐색 행동: {len(run['actions'])}회", f"- 결함 후보: {len(run['findings'])}건", "",
             "관찰된 계약 위반과 재현 결과입니다. 내부 코드의 근본 원인을 확정한 보고서는 아닙니다.", ""]
    if run.get("error"):
        lines += ["## 실행 오류", "", run["error"], ""]
    if not run["findings"]:
        lines += ["이번 실행에서 확인한 계약 위반은 없습니다. 전체 서비스가 정상이라는 보장은 아닙니다.", ""]
    for finding in run["findings"]:
        lines += ["## " + finding["title"], "", f"- 재현: {finding['verification']}", f"- 심각도: {finding.get('severity', 'high')}",
                  "- 기대: " + finding["expected"], "- 관찰: " + finding["actual"], "",
                  "### 재현 절차", "", "초기 화면에서 아래 동작을 순서대로 실행합니다.", ""]
        for i, action in enumerate(run["actions"][:finding["step"]], 1):
            value = f" → {action['value']}" if action["value"] else ""
            lines.append(f"{i}. {action['kind']} · {action['target']}{value}")
        lines += ["", f"![실행 전]({finding['before']})", f"![실행 후]({finding['after']})", ""]
    lines += ["## 기록", "", f"[탐색 Trace](/artifacts/{run['id']}/explore/trace.zip)" if run.get("trace_enabled", True) else "테스트 인증 사용 실행은 인증값 보호를 위해 Trace를 저장하지 않습니다."]
    if run["replays"] and run.get("trace_enabled", True):
        lines.append(f"[재현 Trace](/artifacts/{run['id']}/replay/trace.zip)")
    return "\n".join(lines) + "\n"


async def execute_run(store: Store, run_id: str, auth_headers=None):
    run = store.get_run(run_id)
    started = time.monotonic()
    run["status"] = "running"
    emit(store, run, "start", "격리된 브라우저 테스트를 시작합니다.")
    try:
        async with asyncio.timeout(300):
            target = Target(store, run, auth_headers)
            run["trace_enabled"] = not bool(auth_headers)
            async with target.open("explore") as browser:
                graph = build_explorer(store, run, browser, target)
                config = {**graph_config("crashcustomer.explore", run, kind=run.get("kind", "demo"), scenario=run.get("label")),
                          "recursion_limit": run["max_steps"] * 5 + 10}
                result = await graph.ainvoke({"actions": [], "stop": False}, config)
                run["result"] = result.get("result", "inconclusive")
                run["browser_errors"] = browser.errors
                run["console"] = browser.console
                run["network"] = browser.responses
                run["requests"] = list({(r["method"], r["path"]): r for r in browser.requests}.values())
                run["injected"] = browser.injected
                run["assets"] = list({a["path"]: a for a in browser.assets}.values())
            if run["findings"]:
                await replay(store, run, target)
            run["status"] = "completed"
            emit(store, run, "complete", "테스트를 완료했습니다. 보고서와 증거를 확인할 수 있습니다.")
    except asyncio.CancelledError:
        run.update(status="interrupted", error="서버 종료로 실행이 중단되었습니다.")
        raise
    except Exception as error:
        run.update(status="error", result="agent_error", error=f"{type(error).__name__}: {str(error)[:2000]}")
        emit(store, run, "error", "Agent 실행 오류입니다. 서비스 결함으로 집계하지 않습니다.")
    finally:
        run["finished_at"] = now()
        run["elapsed_seconds"] = round(time.monotonic() - started, 2)
        run = scrub(run, [v.removeprefix("Bearer ") for v in (auth_headers or {}).values()])
        store.save_run(run)
        directory = store.artifacts / run_id
        directory.mkdir(exist_ok=True)
        (directory / "report.md").write_text(report_markdown(run), encoding="utf-8")
