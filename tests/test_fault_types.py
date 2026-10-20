import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import BaseModel

from crashcustomer.browser import BrowserTools, mutate_body, parse_body
from crashcustomer.checks import check_initial, check_resilience
from crashcustomer.probes import invalid_variants, load_probe, load_targets, stages_for, validation_probe

URL = "http://127.0.0.1:8720/api/orders"


def snap(**extra):
    base = {"step": 1, "screenshot": "/s.png", "network": [], "statuses": [], "alerts": [], "busy": False,
            "text": "주문\n주문하기", "error_count": 0, "injected": [], "requests_window": [],
            "fields": [{"label": "이메일", "type": "email", "value": "qa@example.com", "checked": False}],
            "buttons": [{"label": "주문하기", "type": "submit", "disabled": False}]}
    return {**base, **extra}


def codes(found):
    return sorted(f["code"] for f in found)


CLICK = {"kind": "click"}


def test_401_needs_login_guidance_not_generic_error():
    failed = [{"method": "POST", "url": URL, "status": 401}]
    generic = snap(network=failed, alerts=["주문에 실패했습니다."])
    assert codes(check_resilience(snap(), generic, CLICK)) == ["auth_expiry_unhandled"]
    guided = snap(network=failed, alerts=["로그인이 만료되었습니다. 다시 로그인해주세요."])
    assert check_resilience(snap(), guided, CLICK) == []


def test_renamed_response_fields_rendering_undefined():
    mutated = snap(injected=[{"mode": "mutate", "method": "POST", "path": "/api/orders"}],
                   network=[{"method": "POST", "url": URL, "status": 201}], statuses=["undefined 주문번호 undefined"])
    assert codes(check_resilience(snap(), mutated, CLICK)) == ["broken_render"]
    handled = {**mutated, "statuses": [], "alerts": ["주문 결과를 확인하지 못했습니다."]}
    assert check_resilience(snap(), handled, CLICK) == []
    assert "broken_render" not in codes(check_resilience(snap(), {**mutated, "injected": []}, CLICK))


def test_retry_storm_counts_requests_to_the_failing_endpoint():
    stock = "http://127.0.0.1:8720/api/stock"
    failing = [{"method": "GET", "url": stock, "status": 503}] * 10
    first = snap(step=0, injected=[{"mode": "status", "method": "GET", "path": "/api/stock"}] * 10, network=failing,
                 requests_window=[{"method": "GET", "path": "/api/stock"}] * 10)
    assert codes(check_initial(first)) == ["retry_storm", "silent_failure"]
    calm = {**first, "requests_window": [{"method": "GET", "path": "/api/stock"}] * 3, "text": "재고 정보를 불러오지 못했습니다."}
    assert check_initial(calm) == []


def test_two_accepted_submissions_are_duplicate_processing():
    accepted = [{"method": "POST", "url": URL, "status": 201}] * 2
    after = snap(network=accepted, mid_flight=[{"label": "주문하기", "enabled": True, "busy": False, "second_click": True}])
    assert codes(check_resilience(snap(), after, CLICK)) == ["double_submit", "duplicate_processing"]
    locked = snap(network=accepted[:1], mid_flight=[{"label": "주문하기", "enabled": False, "busy": False}])
    assert check_resilience(snap(), locked, CLICK) == []


def test_blocked_assets_need_visible_notice_even_after_reload():
    blocked = [{"mode": "asset_block", "method": "GET", "path": "/assets/app.js"}]
    assert codes(check_initial(snap(step=0, injected=blocked))) == ["asset_failure_silent"]
    banner = "화면을 불러오지 못했습니다. 새로고침해주세요.\n주문하기"
    assert check_initial(snap(step=0, injected=blocked, text=banner)) == []
    reloaded = snap(injected=blocked, text=banner)
    assert check_resilience(snap(text=banner), reloaded, CLICK) == []


def test_page_without_injection_has_no_initial_findings():
    assert check_initial(snap(step=0, network=[{"method": "GET", "url": URL, "status": 500}])) == []


def test_mutate_renames_fields_and_keeps_non_json():
    assert json.loads(mutate_body('{"order_id": 7, "message": "ok"}')) == {"order_id_v2": 7, "message_v2": "ok"}
    assert json.loads(mutate_body("[1,2]")) == {"items_v2": [1, 2]}
    assert mutate_body("<html>") == "<html>"
    assert parse_body('{"a": 1}') == {"a": 1} and parse_body("a=1") is None and parse_body(None) is None


def test_status_faults_repeat_only_the_configured_number_of_times():
    tools = BrowserTools("http://127.0.0.1:8720/shop", Path("/tmp/unused"), "t", "explore", scope="origin",
                         faults=[{"method": "GET", "path": "/api/stock", "mode": "status", "status": 503, "times": 3}])
    request = SimpleNamespace(url="http://127.0.0.1:8720/api/stock", method="GET", resource_type="fetch")
    for _ in range(3):
        tools.matching_fault(request)["remaining"] -= 1
    assert tools.matching_fault(request) is None
    page = BrowserTools("http://127.0.0.1:8720/shop", Path("/tmp/unused"), "t", "explore", scope="origin",
                        faults=[{"mode": "asset_block"}, {"mode": "duplicate", "method": "POST", "path": "/api/orders"}])
    assert page.page_faults == {"asset_block"} and page.duplicate_armed


class Order(BaseModel):
    name: str
    quantity: int


def shop(strict: bool, delay: float = 0) -> FastAPI:
    app = FastAPI()
    lock = asyncio.Lock()

    if strict:
        @app.post("/api/orders")
        def strict_order(order: Order):
            return {"ok": True}
    else:
        @app.post("/api/orders")
        async def lenient_order(request: Request):
            body = await request.json()
            return {"total": int(body["quantity"]) * len(body["name"])}

    @app.get("/api/stock")
    async def stock():
        async with lock:
            await asyncio.sleep(delay)
        return {"stock": 1}
    return app


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")


def test_invalid_variants_cover_missing_wrong_type_and_oversize():
    labels = [label for label, _ in invalid_variants({"name": "a", "quantity": 1})]
    assert labels == ["빈 본문", "name 누락", "name 1만 자", "name 형식 오류", "quantity 누락", "quantity 형식 오류"]


async def test_validation_probe_reports_5xx_once_per_endpoint():
    requests = [{"method": "POST", "path": "/api/orders", "body": {"name": "a", "quantity": 1}}, {"method": "GET", "path": "/api/stock"}]
    async with client_for(shop(strict=False)) as client:
        lenient = await validation_probe(client, requests)
    assert len(lenient["findings"]) == 1 and "5xx" in lenient["findings"][0]["actual"]
    assert {r["variant"] for r in lenient["results"] if r["status"] >= 500} >= {"빈 본문", "quantity 형식 오류"}
    async with client_for(shop(strict=True)) as client:
        strict = await validation_probe(client, requests)
    assert strict["findings"] == [] and all(r["status"] < 500 for r in strict["results"])


async def test_load_probe_finds_first_stage_over_target():
    async with client_for(shop(strict=True, delay=.03)) as client:
        result = await load_probe(client, ["/api/stock"], [1, 8], slo_ms=60, requests_per_user=2)
    assert [stage["users"] for stage in result["stages"]] == [1, 8]
    assert result["saturation_users"] == 8 and result["findings"][0]["code"] == "load_slo_breach"
    async with client_for(shop(strict=True)) as client:
        fast = await load_probe(client, ["/api/stock"], [1, 4], slo_ms=1000, requests_per_user=2)
    assert fast["saturation_users"] is None and fast["findings"] == []


def test_load_targets_use_reads_and_page_only():
    requests = [{"method": "POST", "path": "/api/orders"}, {"method": "GET", "path": "/api/stock"}]
    assert load_targets("http://127.0.0.1:8720/shop", requests) == ["/api/stock", "/shop"]
    assert stages_for(20) == [1, 5, 10, 20] and stages_for(3) == [1, 3]


def test_quiet_successful_retry_is_not_silent_failure():
    stock = "http://127.0.0.1:8720/api/stock"
    recovered = snap(step=0, injected=[{"mode": "status", "method": "GET", "path": "/api/stock"}],
                     network=[{"method": "GET", "url": stock, "status": 503}, {"method": "GET", "url": stock, "status": 200}],
                     requests_window=[{"method": "GET", "path": "/api/stock"}] * 2)
    assert check_initial(recovered) == []
    retried_order = snap(network=[{"method": "POST", "url": URL, "status": 503}, {"method": "POST", "url": URL, "status": 201}],
                         statuses=["주문이 완료되었습니다."])
    assert check_resilience(snap(), retried_order, CLICK) == []


def test_explorer_sees_field_constraints_and_rejections():
    from crashcustomer.policy import available_actions
    snapshot = {"buttons": [], "fields": [{"label": "수량", "type": "number", "value": "12", "valid": False, "required": True,
                                          "disabled": False, "checked": False, "min": "1", "max": "10", "message": "값은 10 이하여야 합니다."}]}
    [choice] = available_actions(snapshot)
    assert choice["min"] == "1" and choice["max"] == "10" and "12" in choice["current_value_rejected"]
