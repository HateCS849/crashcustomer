"""One controlled target. Actor observes the browser; it never receives this fault switch."""


def render_demo(token: str, fault: str) -> str:
    # Fault code is part of the application under test. The actor sees rendered UI only,
    # never page source, the fault name, the database, or the evaluator's answer key.
    on_error = {
        "healthy": "errorBox.textContent = data.message;",
        "reset": "form.reset(); errorBox.textContent = data.message;",
        "locked": "errorBox.textContent = data.message; locked = true;",
        "false_success": "successBox.textContent = '신청이 완료되었습니다.'; form.hidden = true;",
    }[fault]
    return TEMPLATE.replace("__TOKEN__", token).replace("__ON_ERROR__", on_error)


TEMPLATE = r'''<!doctype html>
<html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BUILD / 26 — 참가 신청</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f1e9;color:#18211e;font-family:system-ui,-apple-system,sans-serif}
main{max-width:1040px;margin:64px auto;padding:0 32px;display:grid;grid-template-columns:1fr 1fr;gap:72px}.tag{font-size:12px;letter-spacing:3px;font-weight:700;color:#5a6b5d}h1{font-size:64px;letter-spacing:-4px;line-height:1.05;margin:26px 0}h1 span{color:#66876a}.intro{color:#66716b;line-height:1.9}hr{border:0;border-top:1px solid #d9ddd1;margin:32px 0}.detail{font-size:14px;line-height:2.2}article{background:#fff;border:1px solid #e2e5dc;border-radius:18px;padding:32px;box-shadow:0 16px 60px #1c332009}h2{margin:0 0 26px;font-size:23px}label{display:block;font-size:13px;font-weight:600;margin:18px 0 8px}input[type=text],input[type=email],select{width:100%;border:1px solid #d7ddd2;border-radius:8px;padding:13px;font:inherit;color:#18211e;background:white}input:focus,select:focus{outline:2px solid #83a48a;outline-offset:2px}.consent{display:flex;align-items:center;gap:9px;line-height:1.6;font-weight:400}button{width:100%;padding:15px;border:0;border-radius:8px;background:#263e2e;color:#fff;font:inherit;font-weight:600;cursor:pointer;margin-top:22px}button:disabled{opacity:.45;cursor:not-allowed}[role=alert]{color:#a53d27;background:#fff2ed;border-radius:8px;font-size:13px;line-height:1.7;padding:0;margin-top:16px}[role=alert]:not(:empty){padding:12px}[role=status]:not(:empty){background:#edf7ed;padding:18px;border-radius:8px;line-height:1.8;color:#285b39}.note{font-size:11px;color:#839084;margin-top:22px;line-height:1.7}.back{font-size:12px;color:#718175;text-decoration:none}@media(max-width:700px){main{grid-template-columns:1fr;gap:24px;margin:32px auto}h1{font-size:48px}}
</style></head><body><main><section><span class="tag">BUILD / 26 · COMMUNITY DAY</span><h1>함께 만드는<br>다음의 <span>가능성.</span></h1><p class="intro">아이디어를 나누고, 새로운 동료를 만나는 하루.<br>개발자 커뮤니티 데이에 초대합니다.</p><hr><div class="detail">일시　2026. 11. 14 · 13:00–18:00<br>장소　성수 커뮤니티 스튜디오<br>참가비　무료 · 사전 신청</div><p class="note">CrashCustomer 전용 테스트 행사입니다.<br>실제 예약이나 외부 전송은 발생하지 않습니다.</p></section>
<article><h2>참가 신청</h2><form id="registration" aria-busy="false"><label for="name">이름</label><input id="name" name="name" type="text" autocomplete="name" required placeholder="홍길동">
<label for="email">이메일</label><input id="email" name="email" type="email" autocomplete="email" required placeholder="you@example.com">
<label for="interest">관심 분야</label><select id="interest" name="interest"><option value="ai">AI / 에이전트</option value="web">웹 개발</option><option value="data">데이터</option></select>
<label class="consent" for="consent"><input id="consent" name="consent" type="checkbox" required>개인정보 수집 및 이용에 동의합니다.</label>
<button type="submit">참가 신청하기</button><div role="alert" aria-live="assertive"></div></form><div role="status" aria-live="polite"></div><p class="note">이름, 이메일, 개인정보 동의는 필수 항목입니다.</p></article></main>
<script>
const form=document.querySelector('form'),button=form.querySelector('button'),errorBox=document.querySelector('[role=alert]'),successBox=document.querySelector('[role=status]');let locked=false;
form.addEventListener('submit',async(event)=>{event.preventDefault();errorBox.textContent='';successBox.textContent='';form.setAttribute('aria-busy','true');button.disabled=true;
try{const response=await fetch('/api/demo/__TOKEN__/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:form.elements.name.value,email:form.elements.email.value,consent:form.elements.consent.checked})});const data=await response.json();
if(!response.ok){__ON_ERROR__}else{successBox.textContent=data.message;form.hidden=true;}}
catch(error){errorBox.textContent='연결에 실패했습니다. 다시 시도해주세요.';}
finally{form.setAttribute('aria-busy','false');if(!locked)button.disabled=false;}});
</script></body></html>'''
