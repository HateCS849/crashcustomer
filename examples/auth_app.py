"""Synthetic Bearer-auth test target. Run on 8721; never use a real credential."""
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

app = FastAPI()
TEST_TOKEN = "crashcustomer-synthetic-token"


def authorize(authorization):
    if authorization != "Bearer " + TEST_TOKEN:
        raise HTTPException(401, "synthetic test token required")


@app.get("/")
def page():
    return HTMLResponse('''<!doctype html><html lang="ko"><meta charset="utf-8"><title>인증 측정 예제</title>
    <h1>인증이 필요한 신청</h1><p id="inventory" role="status"></p>
    <form><label>이름<input name="name" required></label><button type="submit">신청</button></form>
    <p id="feedback" role="alert"></p><script>
    fetch('/private').then(async r=>{document.querySelector('#inventory').textContent=r.ok?'신청 가능':'로그인이 필요합니다.'});
    document.querySelector('form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;
    try {const r=await fetch('/private',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:e.target.name.value})});
    document.querySelector('#feedback').textContent=r.ok?'신청이 완료되었습니다.':'로그인이 필요합니다. 다시 시도하세요.';}
    catch(_){document.querySelector('#feedback').textContent='오류입니다. 다시 시도하세요.'}finally{b.disabled=false}};
    </script></html>''')


@app.get("/private")
def read(authorization: str | None = Header(default=None)):
    authorize(authorization)
    return {"available": True}


@app.post("/private")
def write(authorization: str | None = Header(default=None)):
    authorize(authorization)
    return {"accepted": True}
