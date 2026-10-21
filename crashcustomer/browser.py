"""Restricted Playwright tools. The planner can reference only currently observed controls.

Two scopes: "demo" reaches only its own built-in session; "origin" (registered apps) reaches the
target's own origin only. Optional fault rules change the browser's view of the network without
changing the application under test:

  status        matching fetch/XHR answered with an HTTP error (503, 500, 401, 429 ...), `times` in a row
  abort         connection reset
  delay         response held back for delay_ms
  mutate        real response with renamed fields (frontend/backend version mismatch)
  duplicate     delayed response and a second click on the same submit button
  slow_network  whole page throttled through Chrome DevTools (latency, bandwidth)
  asset_block   same-origin scripts and stylesheets fail to load
"""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .config import browser_options
from .models import Action
from .secrets import scrub

OBSERVE_JS = """() => {
 const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
 const label = e => e.getAttribute('aria-label') || Array.from(e.labels || []).map(l=>l.innerText.trim()).join(' ') || e.getAttribute('placeholder') || '';
 return {
   title: document.title,
   fields: Array.from(document.querySelectorAll('input,select,textarea')).filter(visible).map(e=>({
     label:label(e),type:e.type||e.tagName.toLowerCase(),value:e.type==='password'?'[redacted]':e.value,
     checked:e.checked||false,disabled:e.disabled,required:e.required,valid:e.validity.valid,
     min:e.min||'',max:e.max||'',maxlength:e.maxLength>0?e.maxLength:null,pattern:e.pattern||'',message:e.validationMessage||'',
     options:e.tagName==='SELECT'?Array.from(e.options).map(o=>({label:o.text,value:o.value})):[]
   })),
   buttons:Array.from(document.querySelectorAll('button,input[type=submit]')).filter(visible).map(e=>({
     label:e.getAttribute('aria-label')||e.innerText.trim()||e.value,disabled:e.disabled,type:e.type
   })),
   alerts:Array.from(document.querySelectorAll('[role=alert]')).filter(visible).map(e=>e.innerText.trim()).filter(Boolean),
   statuses:Array.from(document.querySelectorAll('[role=status]')).filter(visible).map(e=>e.innerText.trim()).filter(Boolean),
   busy:!!document.querySelector('[aria-busy="true"]'),
   text:(document.body?.innerText||'').slice(0,4000)
 };
}"""


def parse_body(raw: str | None):
    if not raw:
        return None
    try:
        value = json.loads(raw[:8192])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def mutate_body(text: str) -> str:
    """Simulate a backend that renamed every response field (e.g. order_id → order_id_v2)."""
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if isinstance(value, dict):
        return json.dumps({f"{key}_v2": item for key, item in value.items()}, ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps({"items_v2": value}, ensure_ascii=False)
    return json.dumps({"value_v2": value})


class BrowserTools:
    def __init__(self, url: str, directory: Path, run_id: str, phase: str, scope: str = "demo", faults: list[dict] | None = None, auth_headers: dict | None = None):
        self.url, self.directory, self.run_id, self.phase, self.scope = url, directory, run_id, phase, scope
        self.auth_headers = auth_headers or {}
        self.secret_values = [v.removeprefix("Bearer ") for v in self.auth_headers.values()]
        self.trace_enabled = not bool(self.auth_headers)
        self.responses, self.console, self.errors, self.blocked = [], [], [], []
        self.requests, self.injected, self.mid_flight = [], [], []
        self.faults = [{**rule, "remaining": rule.get("times", 1)} for rule in faults or []]
        self.page_faults = {rule["mode"] for rule in faults or [] if rule["mode"] in {"slow_network", "asset_block"}}
        self.duplicate_armed = any(rule["mode"] == "duplicate" for rule in faults or [])
        self.inflight, self.assets = set(), []
        self.network_cursor = self.injected_cursor = self.mid_flight_cursor = self.request_cursor = 0
        self.pw = self.browser = self.context = self.page = None

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(**browser_options())
            self.context = await self.browser.new_context(viewport={"width": 1280, "height": 900}, locale="ko-KR", extra_http_headers=self.auth_headers)
            if self.trace_enabled:
                await self.context.tracing.start(screenshots=True, snapshots=True, sources=False)
            self.page = await self.context.new_page()
            self.page.set_default_timeout(5000)
            self.page.on("response", self._response)
            self.page.on("request", self._request)
            self.page.on("requestfailed", self._failed)
            self.page.on("requestfinished", lambda request: self.inflight.discard(id(request)))
            self.page.on("console", lambda msg: self.console.append({"type": msg.type, "text": scrub(msg.text[:1500], self.secret_values)}))
            self.page.on("pageerror", lambda error: self.errors.append(scrub(str(error)[:1500], self.secret_values)))
            await self.context.route("**/*", self._guard_request)
            if "slow_network" in self.page_faults:
                cdp = await self.context.new_cdp_session(self.page)
                await cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 800,
                               "downloadThroughput": 50 * 1024, "uploadThroughput": 20 * 1024})
                self.injected.append({"mode": "slow_network", "method": "*", "path": "*"})
            await self.page.goto(self.url, wait_until="domcontentloaded")
            return self
        except BaseException:
            await self._close()
            raise

    def allowed(self, url: str) -> bool:
        expected, actual = urlsplit(self.url), urlsplit(url)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            return False
        if self.scope == "origin":
            return True
        token = expected.path.rsplit("/", 1)[-1]
        return actual.path in {expected.path, f"/api/demo/{token}/submit", "/favicon.ico"}

    def matching_fault(self, request) -> dict | None:
        if getattr(request, "resource_type", "fetch") not in {"fetch", "xhr"}:
            return None
        path = urlsplit(request.url).path
        for rule in self.faults:
            if rule["remaining"] > 0 and rule["method"] == request.method and path == rule["path"]:
                return rule
        return None

    async def _guard_request(self, route):
        request = route.request
        if not self.allowed(request.url):
            self.blocked.append(request.url)
            await route.abort()
            return
        if "asset_block" in self.page_faults and request.resource_type in {"script", "stylesheet"}:
            path = urlsplit(request.url).path
            self.injected.append({"mode": "asset_block", "method": "GET", "path": path})
            await route.abort("failed")
            return
        rule = self.matching_fault(request)
        if not rule:
            await route.continue_()
            return
        rule["remaining"] -= 1
        self.injected.append({"method": rule["method"], "path": rule["path"], "mode": rule["mode"], "status": rule.get("status")})
        if rule["mode"] == "status":
            status = rule.get("status", 503)
            headers = {"Retry-After": "2"} if status == 429 else {}
            message = {401: "인증이 만료되었습니다.", 429: "요청이 너무 많습니다."}.get(status, "일시적 장애입니다.")
            await route.fulfill(status=status, content_type="application/json", headers=headers,
                                body=json.dumps({"message": message, "injected_by": "crashcustomer"}, ensure_ascii=False))
        elif rule["mode"] == "abort":
            await route.abort("connectionreset")
        elif rule["mode"] == "mutate":
            response = await route.fetch(max_redirects=0)
            await route.fulfill(response=response, body=mutate_body(await response.text()))
        else:
            await asyncio.sleep(rule.get("delay_ms", 3000) / 1000)
            await route.continue_()

    def _request(self, request):
        if not self.allowed(request.url):
            return
        if request.resource_type in {"script", "stylesheet"}:
            self.assets.append({"type": request.resource_type, "path": urlsplit(request.url).path})
        if request.resource_type in {"fetch", "xhr"}:
            self.inflight.add(id(request))
            self.requests.append({"method": request.method, "path": urlsplit(request.url).path, "query": scrub(urlsplit(request.url).query, self.secret_values), "body": scrub(parse_body(request.post_data), self.secret_values)})

    def _response(self, response):
        if response.request.resource_type in {"fetch", "xhr"}:
            self.responses.append({"url": scrub(response.url, self.secret_values), "content_type": response.headers.get("content-type", ""), "method": response.request.method, "status": response.status})

    def _failed(self, request):
        self.inflight.discard(id(request))
        if request.resource_type in {"fetch", "xhr"} and self.allowed(request.url):
            self.responses.append({"url": request.url, "method": request.method, "status": 0, "failure": request.failure})

    async def observe(self, step: int) -> dict:
        # The target marks actual async work with aria-busy; a disabled button alone is not a loading signal.
        busy_timeout = False
        try:
            await self.page.locator('[aria-busy="true"]').wait_for(state="hidden", timeout=8000)
        except Exception:
            if self.scope == "demo":
                raise
            busy_timeout = True
        if self.scope == "origin":
            try:
                await self.page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            # networkidle ignores requests held in a route (delay faults); wait for them, then let the UI settle.
            for _ in range(80):
                if not self.inflight:
                    break
                await asyncio.sleep(.1)
            await asyncio.sleep(.3)
        snapshot = await self.page.evaluate(OBSERVE_JS)
        snapshot["busy_timeout"] = busy_timeout
        snapshot["injected"] = self.injected[self.injected_cursor:]
        self.injected_cursor = len(self.injected)
        snapshot["mid_flight"] = self.mid_flight[self.mid_flight_cursor:]
        self.mid_flight_cursor = len(self.mid_flight)
        snapshot["error_count"] = len(self.errors)
        snapshot["requests_window"] = [{"method": r["method"], "path": r["path"]} for r in self.requests[self.request_cursor:]]
        self.request_cursor = len(self.requests)
        snapshot["aria"] = (await self.page.locator("body").aria_snapshot())[:12000]
        snapshot["network"] = self.responses[self.network_cursor:]
        self.network_cursor = len(self.responses)
        snapshot["step"] = step
        filename = f"{step:02d}.png"
        await self.page.screenshot(path=str(self.directory / filename), full_page=True,
                                   mask=[self.page.locator("input[type=password]")] + [self.page.get_by_text(v, exact=False) for v in self.secret_values if v])
        snapshot["screenshot"] = f"/artifacts/{self.run_id}/{self.phase}/{filename}"
        snapshot["console_errors"] = self.errors[-5:]
        return scrub(snapshot, self.secret_values)

    async def act(self, action: Action, snapshot: dict):
        if action.kind == "finish":
            return
        collection = snapshot["buttons"] if action.kind == "click" else snapshot["fields"]
        controls = [item for item in collection if item["label"] == action.target]
        if len(controls) != 1 or controls[0]["disabled"]:
            raise ValueError("관찰된 사용 가능한 컨트롤 하나를 정확히 지정해야 합니다: " + action.target)
        control = controls[0]
        if action.kind == "click":
            button = self.page.get_by_role("button", name=action.target, exact=True)
            await button.click()
            if self.scope == "origin" and self.duplicate_armed and control.get("type") == "submit":
                # Impatient user: second click while the first request is still pending.
                self.duplicate_armed = False
                await asyncio.sleep(.3)
                try:
                    await button.click(timeout=800)
                    self.mid_flight.append({"label": action.target, "enabled": True, "busy": False, "second_click": True})
                except Exception:
                    self.mid_flight.append({"label": action.target, "enabled": False, "busy": False, "second_click": False})
            elif self.scope == "origin":
                await self._check_in_flight(button, action.target)
        else:
            locator = self.page.get_by_label(action.target, exact=True)
            if await locator.count() != 1:
                locator = self.page.get_by_placeholder(action.target, exact=True)
            if action.kind == "fill":
                if control["type"] not in {"text", "email", "tel", "textarea", "url", "number"}:
                    raise ValueError("텍스트 입력 필드가 아닙니다.")
                await locator.fill(action.value)
            elif action.kind == "check":
                if control["type"] != "checkbox":
                    raise ValueError("체크박스가 아닙니다.")
                await locator.check()
            elif action.kind == "select":
                await locator.select_option(action.value)

    async def _check_in_flight(self, button, label: str):
        """While a request is still pending, record whether the user could submit again."""
        await asyncio.sleep(.4)
        if not self.inflight:
            return
        try:
            enabled = await button.is_enabled(timeout=1000) and await button.is_visible()
        except Exception:
            enabled = False
        busy = await self.page.locator('[aria-busy="true"]').count() > 0
        self.mid_flight.append({"label": label, "enabled": enabled, "busy": busy})

    async def __aexit__(self, *_):
        await self._close()

    async def _close(self):
        try:
            if self.context:
                try:
                    if self.trace_enabled:
                        await self.context.tracing.stop(path=str(self.directory / "trace.zip"))
                finally:
                    await self.context.close()
        finally:
            try:
                if self.browser:
                    await self.browser.close()
            finally:
                if self.pw:
                    await self.pw.stop()
