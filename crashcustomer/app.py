import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .agent import execute_run, report_markdown
from .attachments import MAX_FILE_BYTES, MAX_TEXT_BYTES, TEST_PLAN_TEMPLATE, AttachmentError, inspect_upload
from .config import BROWSER_SLOTS, DATA_DIR, DEFAULT_BACKEND, OLLAMA_MODEL, OLLAMA_URL, ROOT
from .demo import render_demo
from .models import CONTRACT, FAULTS, Applicant, ProjectRequest, RunRequest
from .store import Store
from .lab import LabManager, LabRequest, lab_report
from .campaign import CampaignManager, CampaignRequest, campaign_report
from .targets import TargetError, allowed_hosts, check_target, health_check
from .tracing import tracing_status
from .events import stream_records
from .routing import route_goal

store = Store(DATA_DIR)
tasks: set[asyncio.Task] = set()
semaphore = asyncio.Semaphore(BROWSER_SLOTS)
lab_manager = LabManager(lambda: store)
campaign_manager = CampaignManager(lambda: store, semaphore, browser_slots=BROWSER_SLOTS)


@asynccontextmanager
async def lifespan(app):
    store.recover_interrupted()
    store.recover_experiments()
    store.recover_campaigns()
    yield
    await campaign_manager.shutdown()
    await lab_manager.shutdown()
    for task in list(tasks):
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="CrashCustomer", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.mount("/artifacts", StaticFiles(directory=store.artifacts), name="artifacts")


@app.get("/api/events/{kind}/{record_id}")
async def live_events(kind: str, record_id: str, request: Request):
    methods = {"runs": store.get_run, "campaigns": store.get_campaign, "lab": store.get_experiment}
    if kind not in methods or not methods[kind](record_id):
        raise HTTPException(404, "실행 기록이 없습니다.")
    def record():
        value = methods[kind](record_id)
        if kind == "campaigns" and value and value.get("status") == "running" and value.get("run_ids"):
            child = store.get_run(value["run_ids"][-1])
            if child:
                value["active_run"] = {"id": child["id"], "status": child["status"], "events": child["events"][-8:], "usage": child["usage"]}
        return value
    return stream_records(request, record)


@app.post("/api/assistant/route")
async def assistant_route(request: dict):
    message = request.get("message", "")
    if not isinstance(message, str) or not 1 <= len(message) <= 1500:
        raise HTTPException(422, "입력은 1~1500자여야 합니다.")
    return route_goal(message)


@app.get("/api/tracing/status")
def trace_status():
    return tracing_status()


@app.post("/api/tracing/verify")
async def trace_verify():
    from .tracing import verify_delivery
    return await verify_delivery(store.directory)


@app.post("/api/tracing/campaigns/{campaign_id}/verify")
async def campaign_trace_verify(campaign_id: str):
    from .tracing import verify_campaign_delivery
    campaign = require_campaign(campaign_id)
    if campaign["status"] in {"queued", "running"}:
        raise HTTPException(409, "캠페인 종료 후 실제 추적을 검증해주세요.")
    return await verify_campaign_delivery(store.directory, campaign)


@app.middleware("http")
async def upload_size_limit(request: Request, call_next):
    if request.method == "POST" and request.url.path.endswith("/attachments"):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_FILE_BYTES + 128 * 1024:
            return JSONResponse({"detail": "파일 하나당 최대 20MB까지 첨부할 수 있습니다."}, status_code=413)
    return await call_next(request)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/api/lab/runs", status_code=202)
async def create_lab_run(request: LabRequest):
    try:
        run = await lab_manager.start(request)
    except ValueError as error:
        raise HTTPException(429, str(error)) from error
    return {"id": run["id"], "status": run["status"]}


@app.get("/api/lab/runs")
def lab_runs():
    return [{key: run.get(key) for key in ("id", "config", "status", "stage", "created_at", "elapsed_seconds")}
            for run in store.list_experiments()]


def require_experiment(run_id):
    run = store.get_experiment(run_id)
    if not run:
        raise HTTPException(404, "실험 기록이 없습니다.")
    return run


@app.get("/api/lab/runs/{run_id}")
def lab_run(run_id: str):
    return require_experiment(run_id)


@app.post("/api/lab/runs/{run_id}/cancel")
async def cancel_lab_run(run_id: str):
    require_experiment(run_id)
    task = lab_manager.tasks.get(run_id)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return require_experiment(run_id)


@app.get("/api/lab/runs/{run_id}/report")
def experiment_report(run_id: str, format: str = "markdown"):
    run = require_experiment(run_id)
    if format == "json":
        import json
        return Response(json.dumps(run, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="lab-{run_id[:8]}.json"'})
    return Response(lab_report(run), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="lab-{run_id[:8]}.md"'})


@app.get("/api/lab/runs/{run_id}/logs/{variant}")
def experiment_logs(run_id: str, variant: str):
    require_experiment(run_id)
    if variant not in {"before", "after"}:
        raise HTTPException(404, "버전이 없습니다.")
    # run_id must originate from SQLite lookup; path components are generated IDs.
    path = store.artifacts / "lab" / run_id / variant / "fallback.jsonl"
    if not path.exists():
        raise HTTPException(404, "파일로 기록된 로그가 없습니다.")
    return FileResponse(path, filename=f"lab-{variant}-fallback.jsonl", media_type="application/octet-stream")


def require_lab_session(session_id):
    session = lab_manager.sessions.get(session_id)
    if not session:
        raise HTTPException(404, "실험 세션이 없습니다.")
    return session


@app.post("/lab/service/{session_id}/apple")
async def mock_apple(session_id: str):
    status, payload = await require_lab_session(session_id).apple()
    return JSONResponse(payload, status_code=status)


@app.api_route("/lab/service/{session_id}/{endpoint}", methods=["GET", "POST"])
async def lab_service(session_id: str, endpoint: str, user: str = "observer"):
    if endpoint not in {"login", "contents", "search"}:
        raise HTTPException(404, "실험 API가 없습니다.")
    status, payload = await require_lab_session(session_id).handle(endpoint, user[:80], session_id)
    return JSONResponse(payload, status_code=status)


@app.get("/api/config")
async def config():
    models, error = [], None
    try:
        async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
            response = await client.get(OLLAMA_URL + "/api/tags")
            response.raise_for_status()
            models = [m["name"] for m in response.json().get("models", [])]
    except Exception:
        error = "Ollama에 연결되지 않았습니다. 규칙 기반 모드로 브라우저 흐름을 검증할 수 있습니다."
    return {"faults": FAULTS, "contract": CONTRACT, "default_backend": DEFAULT_BACKEND,
            "ollama": {"model": OLLAMA_MODEL, "available": OLLAMA_MODEL in models, "models": models, "message": error},
            "tracing": tracing_status(), "allowed_hosts": sorted(allowed_hosts())}


@app.get("/api/runs")
def list_runs():
    # Keep the history endpoint small; evidence is fetched only for the selected run.
    return [{k: r.get(k) for k in ("id", "status", "fault", "backend", "goal", "created_at", "result", "elapsed_seconds", "kind", "label", "campaign_id")}
            | {"finding_count": len(r["findings"])} for r in store.list_runs()]


@app.post("/api/runs", status_code=202)
async def start_run(request: RunRequest):
    if request.project_id:
        require_project(request.project_id)
        raise HTTPException(409, "등록한 프로젝트는 사전 점검(POST /api/projects/{id}/campaigns)으로 실행합니다. 내장 데모를 대신 실행하지 않습니다.")
    if len(tasks) >= 3:
        raise HTTPException(429, "실행 대기열이 가득 찼습니다. 진행 중인 실행이 끝난 뒤 다시 시도하세요.")
    if request.backend == "ollama":
        state = await config()
        if not state["ollama"]["available"]:
            raise HTTPException(503, f"Ollama 모델 {OLLAMA_MODEL}을 사용할 수 없습니다. Ollama와 모델을 준비해주세요.")
    run = store.create_run(request.model_dump())

    async def work():
        async with semaphore:
            await execute_run(store, run["id"])

    task = asyncio.create_task(work())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return {"id": run["id"], "status": run["status"]}


def require_project(project_id: str) -> dict:
    project = store.get_project(project_id)
    if not project:
        raise HTTPException(404, "프로젝트가 없습니다.")
    return project


def execution(project: dict) -> dict:
    try:
        check_target(project.get("browser_url", ""))
        return {"status": "ready", "message": "로컬·허용 호스트입니다. 사전 점검을 실행할 수 있습니다."}
    except TargetError as error:
        return {"status": "blocked", "message": str(error)}


@app.get("/api/projects")
def list_projects():
    return [{**project, "execution": execution(project)} for project in store.list_projects()]


@app.get("/api/projects/{project_id}/connection")
async def project_connection(project_id: str):
    project = require_project(project_id)
    state = execution(project)
    if state["status"] != "ready":
        return {**state, "health": None}
    return {**state, "health": await health_check(project["browser_url"])}


@app.post("/api/projects/{project_id}/campaigns", status_code=202)
async def start_campaign(project_id: str, request: CampaignRequest):
    project = require_project(project_id)
    try:
        check_target(project.get("browser_url", ""))
    except TargetError as error:
        raise HTTPException(400, str(error)) from error
    health = await health_check(project["browser_url"])
    if not health["ok"]:
        raise HTTPException(503, "대상 앱이 응답하지 않아 점검을 시작하지 않았습니다. " + health["message"])
    if request.backend == "ollama" and not (await config())["ollama"]["available"]:
        raise HTTPException(503, f"Ollama 모델 {OLLAMA_MODEL}을 사용할 수 없습니다. Ollama와 모델을 준비해주세요.")
    try:
        campaign = campaign_manager.start(project, request)
    except ValueError as error:
        raise HTTPException(429, str(error)) from error
    return {"id": campaign["id"], "status": campaign["status"]}


@app.get("/api/projects/{project_id}/campaigns")
def project_campaigns(project_id: str):
    require_project(project_id)
    return [{k: c.get(k) for k in ("id", "status", "stage", "created_at", "elapsed_seconds", "summary", "config")}
            for c in store.list_campaigns(project_id)]


def require_campaign(campaign_id: str) -> dict:
    campaign = store.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "사전 점검 기록이 없습니다.")
    return campaign


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str):
    return require_campaign(campaign_id)


@app.get("/api/campaigns/{campaign_id}/report")
def get_campaign_report(campaign_id: str):
    campaign = require_campaign(campaign_id)
    return Response(campaign_report(campaign), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="campaign-{campaign_id[:8]}.md"'})


@app.post("/api/projects", status_code=201)
def create_project(request: ProjectRequest):
    project = store.create_project(request.model_dump())
    return {**project, "execution": execution(project)}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    project = require_project(project_id)
    return {**project, "execution": execution(project), "attachments": store.list_attachments(project_id)}


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, request: ProjectRequest):
    require_project(project_id)
    project = store.update_project(project_id, request.model_dump(exclude_unset=True))
    return {**project, "execution": execution(project)}


@app.get("/api/attachment-templates/test-plan")
def test_plan_template():
    return Response(TEST_PLAN_TEMPLATE, media_type="text/yaml; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="test-plan.yaml"'})


@app.post("/api/projects/{project_id}/attachments", status_code=201)
async def upload_attachment(project_id: str, file: UploadFile = File(...), category: str = Form("auto")):
    try:
        require_project(project_id)
        chunks, size = [], 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise HTTPException(413, "파일 하나당 최대 20MB까지 첨부할 수 있습니다.")
            chunks.append(chunk)
        content = b"".join(chunks)
        try:
            analysis = await run_in_threadpool(inspect_upload, file.filename or "", content, category)
            return await run_in_threadpool(store.add_attachment, project_id, file.filename, content, analysis)
        except (AttachmentError, ValueError) as error:
            raise HTTPException(400, str(error)) from error
    finally:
        await file.close()


def require_attachment(project_id: str, attachment_id: str):
    require_project(project_id)
    attachment = store.get_attachment(project_id, attachment_id)
    if not attachment:
        raise HTTPException(404, "첨부 파일이 없습니다.")
    return attachment


@app.get("/api/projects/{project_id}/attachments/{attachment_id}/download")
def download_attachment(project_id: str, attachment_id: str):
    attachment = require_attachment(project_id, attachment_id)
    return FileResponse(store.uploads / attachment_id, filename=attachment["filename"],
                        media_type="application/octet-stream", headers={"X-Content-Type-Options": "nosniff"})


@app.get("/api/projects/{project_id}/attachments/{attachment_id}/preview")
def preview_attachment(project_id: str, attachment_id: str):
    attachment = require_attachment(project_id, attachment_id)
    text = None
    if attachment["format"] not in {"zip", "pdf", "xlsx"}:
        path = store.uploads / attachment_id
        if path.stat().st_size <= MAX_TEXT_BYTES:
            text = path.read_text(encoding="utf-8-sig")[:12000]
    return {**attachment, "text": text}


@app.delete("/api/projects/{project_id}/attachments/{attachment_id}", status_code=204)
def remove_attachment(project_id: str, attachment_id: str):
    require_attachment(project_id, attachment_id)
    store.remove_attachment(project_id, attachment_id)
    return Response(status_code=204)


def require_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "실행 기록이 없습니다.")
    return run


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return require_run(run_id)


@app.get("/api/runs/{run_id}/report")
def get_report(run_id: str):
    run = require_run(run_id)
    return Response(report_markdown(run), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="crashcustomer-{run_id[:8]}.md"'})


@app.post("/api/demo-sessions", status_code=201)
def preview_session(request: RunRequest):
    token = store.create_session(request.fault)
    return {"url": f"/demo/{token}"}


@app.get("/demo/{token}", response_class=HTMLResponse)
def demo(token: str):
    session = store.get_session(token)
    if not session:
        raise HTTPException(404, "신청 페이지를 찾을 수 없습니다.")
    return HTMLResponse(render_demo(token, session["fault"]), headers={"Cache-Control": "no-store"})


@app.post("/api/demo/{token}/submit")
def submit(token: str, applicant: Applicant):
    status, payload = store.submit(token, applicant.model_dump())
    return JSONResponse(payload, status_code=status)
