const $ = id => document.getElementById(id);
const faultNames = {healthy:'정상 버전',reset:'입력값 소실',locked:'재시도 불가',false_success:'거짓 성공'};
const stateNames = {queued:'실행 대기',running:'탐색 중',replaying:'재현 중',completed:'완료',error:'실행 오류',interrupted:'중단됨'};
let selected = null, current = null, history = [], config = null, busy = false;
const escapeHTML = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const active = r => r && ['queued','running','replaying'].includes(r.status);
const time = s => new Date(s).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
async function api(path, options={}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : response.status===422 ? '목표와 실행 방식을 확인한 뒤 다시 시도해주세요.' : '요청을 처리하지 못했습니다.');
  return data;
}
function showError(message) { $('form-error').textContent = message; $('form-error').hidden = !message; }
function backendNote() {
  $('backend-note').textContent = $('backend').value === 'rule'
    ? '규칙 기반 모드는 AI를 사용하지 않는 비교 기준선입니다.'
    : config?.ollama.available ? `${config.ollama.model} · 실제 LLM이 화면을 보고 다음 행동을 결정합니다.`
    : `Ollama 모델 ${config?.ollama.model || ''} 연결이 필요합니다. README의 설정을 확인하세요.`;
}
function renderHistory() {
  $('history-count').textContent = history.length;
  $('metric-runs').innerHTML = `${history.length}<small> runs</small>`;
  $('metric-complete').innerHTML = `${history.filter(r=>r.status==='completed').length}<small> runs</small>`;
  $('metric-findings').innerHTML = `${history.reduce((n,r)=>n+r.finding_count,0)}<small> findings</small>`;
  if (!history.length) return;
  $('history').innerHTML = history.map(r=>`<button class="history-item ${selected===r.id?'selected':''}" data-run="${escapeHTML(r.id)}"><span class="history-dot ${r.status==='error'?'error':''}"></span><span><strong>${escapeHTML(r.label || faultNames[r.fault])}</strong><small>${escapeHTML(time(r.created_at))} · ${escapeHTML(stateNames[r.status])} · ${r.backend==='rule'?'RULE':'AI'}</small></span></button>`).join('');
}
function renderRun(run) {
  current = run;
  window.crashEvents.watch('demo', 'runs', run, renderRun);
  const running = active(run);
  $('start-button').disabled = busy || running;
  $('start-button').firstElementChild.textContent = running ? '테스트 실행 중…' : '테스트 시작하기';
  $('metric-state').textContent = stateNames[run.status] || run.status;
  $('run-state').textContent = `${run.backend==='rule'?'규칙 기반':'Ollama AI'} · ${stateNames[run.status]}`;
  $('run-state').className = `run-state ${run.status}`;
  $('step-count').textContent = `${run.actions.length} actions`;
  $('elapsed').textContent = run.elapsed_seconds != null ? `${run.elapsed_seconds.toFixed(1)}초 · ${run.usage.calls} LLM calls` : '실행 중 · 실제 기록';
  $('finding-count').textContent = run.findings.length;
  $('report-link').hidden = running;
  $('report-link').href = `/api/runs/${run.id}/report`;
  const replayObs = run.replays.at(-1)?.observations || [];
  const observation = (replayObs.length ? replayObs : run.observations).at(-1);
  if (observation) {
    $('browser-empty').hidden = true;
    $('screenshot-link').hidden = false;
    $('screenshot-link').href = observation.screenshot;
    $('screenshot').src = observation.screenshot;
    $('observation-label').textContent = `${replayObs.length?'독립 재현':'탐색'} · 관찰 ${observation.step} · 실제 브라우저 화면`;
  } else { $('browser-empty').hidden = false; $('screenshot-link').hidden = true; $('observation-label').textContent='스크린샷 · 화면 구조 · 네트워크 응답'; }
  $('browser-address').textContent = run.target_url || '브라우저 세션 준비 중';
  if (run.error) {
    $('findings').innerHTML = `<div class="error-result"><strong>Agent 실행 오류 · 서비스 결함 아님</strong><br>${escapeHTML(run.error)}<br>브라우저 설치 또는 모델 연결 상태를 확인해주세요.</div>`;
  } else if (run.findings.length) {
    $('findings').innerHTML = run.findings.map(f=>`<article class="finding"><div class="finding-head"><h3>${escapeHTML(f.title)}</h3><span class="verification ${f.verification!=='confirmed'?'unconfirmed':''}">${f.verification==='confirmed'?'독립 재현 확인':f.verification==='pending'?'재현 대기':'재현 미확인'}</span></div><p><b>기대</b> ${escapeHTML(f.expected)}<br><b>관찰</b> ${escapeHTML(f.actual)}</p><div class="evidence-links"><a href="${escapeHTML(f.before)}" target="_blank" rel="noopener">실행 전 ↗</a><a href="${escapeHTML(f.after)}" target="_blank" rel="noopener">실행 후 ↗</a>${f.replay_after?`<a href="${escapeHTML(f.replay_after)}" target="_blank" rel="noopener">재현 증거 ↗</a>`:''}${run.trace_enabled === false ? '<span>인증 보호 · Trace 미저장</span>' : `<a href="/artifacts/${run.id}/explore/trace.zip">Trace ↓</a>`}</div><details><summary>재현 동작 ${f.step}개 확인</summary><ol>${run.actions.slice(0,f.step).map(a=>`<li>${escapeHTML(a.kind)} · ${escapeHTML(a.target)}${a.value?` → ${escapeHTML(a.value)}`:''}</li>`).join('')}</ol></details></article>`).join('');
  } else {
    const done = run.status==='completed';
    const text = done ? (run.result==='goal_completed'?'검사한 경로에서 신청을 완료했습니다.':'탐색을 종료했습니다. 정상 여부를 확정하지 않았습니다.') : '현재 화면을 탐색하며 계약 위반을 확인하고 있습니다.';
    $('findings').innerHTML = `<div class="compact-empty"><span>${done?'✓':'◎'}</span><p>${escapeHTML(text)}<small>${done?'이번 실행의 관찰 결과이며, 전체 서비스의 무결성을 보장하지 않습니다.':'의심 현상을 발견하면 새 환경에서 다시 재현합니다.'}</small></p></div>`;
  }
  if (run.events.length) {
    $('timeline').innerHTML = run.events.slice().reverse().map(e=>`<li><time>${escapeHTML(time(e.time))}</time><div><b>${escapeHTML(e.node)}</b>${escapeHTML(e.message)}</div></li>`).join('');
  } else $('timeline').innerHTML = '<li class="timeline-placeholder">격리된 브라우저 실행을 기다리고 있습니다.</li>';
}
async function refresh() {
  try {
    history = await api('/api/runs');
    if (!selected && history.length) selected = history[0].id;
    renderHistory();
    if (selected) renderRun(await api(`/api/runs/${selected}`));
  } catch (error) { showError('서버 연결을 확인해주세요. ' + error.message); }
}
async function poll() { if (!window.crashEvents.connected('demo')) await refresh(); setTimeout(poll, 3500); }
$('history').addEventListener('click', async event=>{
  const button=event.target.closest('[data-run]');
  if (button) { window.crashProjects?.selectDemo(); selected=button.dataset.run; await refresh(); }
});
window.crashRuns = {show: async id => { selected = id; await refresh(); }};
$('backend').addEventListener('change',backendNote);
$('run-form').addEventListener('submit',async event=>{
  event.preventDefault(); if (busy || active(current)) return;
  if (window.crashProjects?.selectedId) { showError('등록한 프로젝트의 실행 연결은 아직 지원하지 않습니다.'); return; }
  busy=true; $('start-button').disabled=true; showError('');
  try {
    const run=await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal:$('goal').value,fault:document.querySelector('input[name=fault]:checked').value,backend:$('backend').value})});
    selected=run.id; await refresh();
  } catch(error) {showError(error.message);}
  finally {busy=false; $('start-button').disabled=!!active(current);}
});
$('preview-button').addEventListener('click',async()=>{
  const tab=window.open('about:blank','_blank'); if(tab) tab.opener=null;
  try {const result=await api('/api/demo-sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fault:document.querySelector('input[name=fault]:checked').value})}); if(tab) tab.location.href=result.url; else showError('팝업 허용 후 다시 열어주세요.');}
  catch(error){if(tab)tab.close();showError(error.message);}
});
(async()=>{try{config=await api('/api/config'); $('backend').value=config.default_backend; $('contract-list').innerHTML=config.contract.map(c=>`<li>${escapeHTML(c)}</li>`).join('');backendNote();}catch(error){showError(error.message);}poll();})();
