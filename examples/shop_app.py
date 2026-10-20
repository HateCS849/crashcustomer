"""Sample app to register in CrashCustomer (separate process, port 8720).

/buggy  common production mistakes in one screen:
        - resets the form on any error and shows the same message for 401/429/5xx
        - no network-error handling (unhandled rejection, nothing shown)
        - no in-flight lock (double submit → two orders)
        - trusts response fields blindly (renamed fields render as "undefined")
        - stock lookup retries 10 times without delay, then silently gives up
        - no fallback when its script fails to load
        - order API parses raw JSON by hand (missing/invalid fields → 500)
        - stock API serializes every request behind one lock (slow under load)
/fixed  the same screen with those handled.

Run: .venv/bin/python examples/shop_app.py
"""
import asyncio
import itertools

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

app = FastAPI(title="Sample Shop")
order_ids = itertools.count(1001)
orders: list[dict] = []
stock_lock = asyncio.Lock()
STOCK = 42


class Order(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    email: str = Field(min_length=3, max_length=120)
    quantity: int = Field(ge=1, le=10)


def save(variant: str, name, email, quantity) -> dict:
    record = {"id": next(order_ids), "variant": variant, "name": name, "email": email, "quantity": quantity}
    orders.append(record)
    return {"order_id": record["id"], "message": "주문이 완료되었습니다."}


@app.post("/api/buggy/orders", status_code=201)
async def create_buggy_order(request: Request):
    body = await request.json()
    # Hand-written parsing: KeyError / ValueError surface as HTTP 500.
    return save("buggy", body["name"], body["email"], int(body["quantity"]))


@app.post("/api/fixed/orders", status_code=201)
def create_fixed_order(order: Order):
    return save("fixed", order.name, order.email, order.quantity)


@app.get("/api/buggy/stock")
async def buggy_stock():
    async with stock_lock:  # one shared lock: requests queue up under concurrency
        await asyncio.sleep(.06)
        return {"stock": STOCK - len(orders)}


@app.get("/api/fixed/stock")
async def fixed_stock():
    await asyncio.sleep(.005)
    return {"stock": STOCK - len(orders)}


@app.get("/api/orders/count")
def order_count():
    return {"count": len(orders), "by_variant": {v: sum(o["variant"] == v for o in orders) for v in ("buggy", "fixed")}}


SCRIPTS = {
    "buggy": """
const form=document.querySelector('form'), errorBox=document.querySelector('[role=alert]'), statusBox=document.querySelector('[role=status]'), stockBox=document.getElementById('stock');
const payload=()=>({name:form.elements.name.value, email:form.elements.email.value, quantity:Number(form.elements.quantity.value)});
(async () => {
  for (let i = 0; i < 10; i++) {
    try { const r = await fetch('/api/buggy/stock'); if (r.ok) { const d = await r.json(); stockBox.textContent = '남은 수량: ' + d.stock + '개'; return; } } catch (e) {}
  }
})();
form.addEventListener('submit', async event => {
  event.preventDefault(); errorBox.textContent='';
  const response = await fetch('/api/buggy/orders', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload())});
  if (response.ok) { const data = await response.json(); statusBox.textContent = data.message + ' 주문번호 ' + data.order_id; form.hidden = true; }
  else { form.reset(); errorBox.textContent = '주문에 실패했습니다.'; }
});""",
    "fixed": """
const form=document.querySelector('form'), button=form.querySelector('button'), errorBox=document.querySelector('[role=alert]'), statusBox=document.querySelector('[role=status]'), stockBox=document.getElementById('stock');
const payload=()=>({name:form.elements.name.value, email:form.elements.email.value, quantity:Number(form.elements.quantity.value)});
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  for (let i = 0; i < 3; i++) {
    try { const r = await fetch('/api/fixed/stock'); if (r.ok) { const d = await r.json(); if (typeof d.stock === 'number') { stockBox.textContent = '남은 수량: ' + d.stock + '개'; return; } break; } } catch (e) {}
    if (i < 2) await wait(150 * 2 ** i);
  }
  stockBox.textContent = '재고 정보를 불러오지 못했습니다. 주문은 계속할 수 있습니다.';
})();
const messages = {401: '로그인이 만료되었습니다. 다시 로그인한 뒤 주문해주세요.', 429: '요청이 많습니다. 잠시 후 다시 시도해주세요.'};
form.addEventListener('submit', async event => {
  event.preventDefault(); errorBox.textContent=''; button.disabled = true; form.setAttribute('aria-busy','true');
  try {
    const response = await fetch('/api/fixed/orders', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload())});
    if (!response.ok) { errorBox.textContent = messages[response.status] || '일시적인 오류로 주문하지 못했습니다. 입력 내용은 그대로 두었으니 다시 시도해주세요.'; return; }
    const data = await response.json();
    if (typeof data.order_id !== 'number' || typeof data.message !== 'string') { errorBox.textContent = '주문 결과를 확인하지 못했습니다. 주문 내역을 확인한 뒤 다시 시도해주세요.'; return; }
    statusBox.textContent = data.message + ' 주문번호 ' + data.order_id; form.hidden = true;
  } catch (error) {
    errorBox.textContent = '네트워크 연결이 끊겨 주문하지 못했습니다. 입력 내용은 그대로 두었으니 다시 시도해주세요.';
  } finally { form.setAttribute('aria-busy','false'); button.disabled = false; }
});""",
}

FALLBACK = {"buggy": "", "fixed": " onerror=\"document.getElementById('boot').textContent='화면을 불러오지 못했습니다. 새로고침해주세요.'\""}

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>Sample Shop · __VARIANT__</title>
<style>body{font-family:system-ui;background:#f5f4ef;margin:0}main{max-width:420px;margin:60px auto;background:#fff;padding:32px;border-radius:14px}
label{display:block;margin:14px 0 6px;font-size:14px;font-weight:600}input{width:100%;padding:10px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box}
button{margin-top:20px;width:100%;padding:12px;border:0;border-radius:6px;background:#22392f;color:#fff;font-weight:600}button:disabled{opacity:.5}
[role=alert]{color:#a33;margin-top:12px}[role=status]{color:#285b39;margin-top:12px}#stock,#boot{font-size:13px;color:#667}</style></head>
<body><main><h1>원두 정기배송 주문</h1><p id="boot"></p><p id="stock"></p><form aria-busy="false">
<label for="name">받는 분</label><input id="name" required>
<label for="email">이메일</label><input id="email" type="email" required>
<label for="quantity">수량</label><input id="quantity" type="number" min="1" max="10" required>
<button type="submit">주문하기</button><div role="alert"></div></form><div role="status"></div></main>
<script src="/assets/__VARIANT__.js"__FALLBACK__></script></body></html>"""


@app.get("/assets/{variant}.js")
def script(variant: str):
    if variant not in SCRIPTS:
        raise HTTPException(404)
    return Response(SCRIPTS[variant], media_type="text/javascript", headers={"Cache-Control": "no-store"})


@app.get("/{variant}", response_class=HTMLResponse)
def page(variant: str):
    if variant not in SCRIPTS:
        raise HTTPException(404)
    html = PAGE.replace("__VARIANT__", variant).replace("__FALLBACK__", FALLBACK[variant])
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8720)
