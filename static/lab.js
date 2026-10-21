(() => {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clock = s => new Date(s).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  const states = {queued:'실험 대기',running:'측정 중',completed:'완료',error:'실행 오류',cancelled:'중단됨',interrupted:'중단됨'};
  const stages = {queued:'대기',plan:'계획',before:'개선 전 측정',after:'개선 후 측정',analyze:'분석',complete:'완료',cancelled:'중단',error:'오류'};
  const variants = {before:'개선 전',after:'개선 후'};
  const variantNotes = {before:'로그인이 DB 커넥션을 잡은 채 외부 인증을 기다리고, 오류 로그도 같은 풀에 동기 기록합니다.',
                        after:'외부 호출에 시간 제한을 두고 DB 커넥션은 호출 이후에만 잡으며, 오류 로그는 제한된 큐로 비동기 기록합니다.'};
  const endpoints = {login:'로그인',contents:'목록',search:'검색'};
  const icons = {pass:'✓',failure:'!',warning:'△',info:'i'};
  const active = r => r && ['queued','running'].includes(r.status);
  let runs = [], selected = null, current = null, busy = false, timer = null;

  async function request(path, options={}) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : response.status === 422 ? '입력 범위를 확인해주세요.' : '요청을 처리하지 못했습니다.');
    return data;
  }
  function showError(message) { el('lab-error').textContent = message; el('lab-error').hidden = !message; }
  const ms = v => v == null ? '-' : `${Math.round(v).toLocaleString('ko-KR')}`;

  function renderHistory() {
    el('lab-history-count').textContent = runs.length;
    el('lab-metric-runs').innerHTML = `${runs.length}<small> runs</small>`;
    el('lab-metric-complete').innerHTML = `${runs.filter(r=>r.status==='completed').length}<small> runs</small>`;
    const last = runs.find(r => r.elapsed_seconds != null);
    el('lab-metric-elapsed').innerHTML = last ? `${last.elapsed_seconds.toFixed(1)}<small> s</small>` : '-';
    if (!runs.length) return;
    el('lab-history').innerHTML = runs.map(r => `<button type="button" class="lab-history-item ${selected===r.id?'selected':''}" data-lab-run="${esc(r.id)}"><span><strong>사용자 ${esc(r.config.users)}명 · 풀 ${esc(r.config.pool_size)} · 지연 ${esc(r.config.external_delay_ms)}ms</strong><small>${esc(clock(r.created_at))} · ${esc(r.config.comparison==='both'?'전/후 비교':variants[r.config.comparison])}</small></span><span class="run-state ${esc(r.status)}">${esc(states[r.status] || r.status)}</span></button>`).join('');
  }

  function renderCase(c, config) {
    if (!c.summary) return `<article class="lab-case"><h3>${esc(variants[c.variant])} <span class="run-state running">측정 중</span></h3><p>${esc(variantNotes[c.variant])}</p></article>`;
    const rows = Object.entries(c.summary.endpoints).map(([name, v]) => {
      const reasons = Object.entries(v.reasons).filter(([k]) => k !== 'ok').map(([k, n]) => `${k} ${n}`).join(', ');
      const slow = name === 'login' && v.max_ms > config.login_slo_ms;
      return `<tr><td>${esc(endpoints[name] || name)}</td><td>${v.total}</td><td>${v.successful}</td><td class="${v.failed?'bad':''}">${v.failed}</td><td>${ms(v.p95_ms)}</td><td class="${slow?'bad':''}">${ms(v.max_ms)}</td><td>${esc(reasons || '-')}</td></tr>`;
    }).join('');
    const pool = c.final.pool, logs = c.final.logs;
    const logLoss = logs.received - logs.db_saved - logs.file_saved;
    return `<article class="lab-case"><h3>${esc(variants[c.variant])} <span class="verification ${c.summary.protected_reads_passed && c.summary.login_slo_passed ? '' : 'unconfirmed'}">${c.summary.protected_reads_passed ? '목록·검색 정상' : '목록·검색 실패'} · ${c.summary.login_slo_passed ? '로그인 목표 충족' : '로그인 목표 초과'}</span></h3><p>${esc(variantNotes[c.variant])} 측정 ${ms(c.duration_ms)}ms</p>
      <div class="lab-table-wrap"><table class="lab-table"><thead><tr><th>API</th><th>요청</th><th>성공</th><th>실패</th><th>P95(ms)</th><th>최대(ms)</th><th>실패 사유</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="lab-stats"><div><span>최대 커넥션 점유</span><strong class="${pool.peak_active>=pool.size?'bad':''}">${pool.peak_active}/${pool.size}</strong></div><div><span>풀 획득 대기 초과</span><strong class="${pool.acquisition_timeouts?'bad':''}">${pool.acquisition_timeouts}회</strong></div><div><span>최대 대기 요청</span><strong>${pool.peak_waiting}</strong></div><div><span>오류 로그 DB / 파일 / 유실</span><strong class="${logLoss?'bad':''}">${logs.db_saved} / ${logs.file_saved} / ${logLoss}</strong></div></div></article>`;
  }

  function renderRun(run) {
    current = run;
    window.crashEvents.watch('lab', 'lab', run, renderRun);
    const running = active(run);
    el('lab-start').disabled = busy || running;
    el('lab-start').firstElementChild.textContent = running ? '실험 진행 중…' : '실험 시작하기';
    el('lab-cancel').hidden = !running;
    el('lab-metric-state').textContent = running ? (stages[run.stage] || states[run.status]) : (states[run.status] || run.status);
    el('lab-state').textContent = `${states[run.status] || run.status}${running && run.stage ? ' · ' + (stages[run.stage] || run.stage) : ''}`;
    el('lab-state').className = `run-state ${run.status}`;
    el('lab-elapsed').textContent = run.elapsed_seconds != null ? `${run.elapsed_seconds.toFixed(1)}초 · 실제 측정` : '실행 중 · 실제 기록';
    if (run.error) {
      el('lab-cases').innerHTML = `<div class="error-result"><strong>실험 실행 오류 · 측정된 서비스 결함 아님</strong><br>${esc(run.error)}</div>`;
    } else if (run.cases?.length) {
      el('lab-cases').innerHTML = run.cases.map(c => renderCase(c, run.config)).join('');
    } else {
      el('lab-cases').innerHTML = '<div class="compact-empty"><span>⌁</span><p>새 DB와 커넥션 풀을 준비하고 있습니다.</p></div>';
    }
    const findings = run.findings || [];
    el('lab-finding-count').textContent = findings.length;
    el('lab-report-links').hidden = running;
    el('lab-report-md').href = `/api/lab/runs/${run.id}/report`;
    el('lab-report-json').href = `/api/lab/runs/${run.id}/report?format=json`;
    let html = findings.map(f => `<div class="lab-finding ${esc(f.severity)}"><i>${icons[f.severity] || 'i'}</i><div><b>${esc(f.title)}</b>${esc(f.detail)}</div></div>`).join('');
    if (run.ai_analysis) {
      const a = run.ai_analysis;
      html += `<div class="lab-ai"><strong>AI 해석 (가설 · 측정값만 근거)</strong><p>${esc(a.summary)}</p>${a.hypotheses.length ? `<ul>${a.hypotheses.map(h => `<li>${esc(h)}</li>`).join('')}</ul>` : ''}${a.next_experiments.length ? `<strong>추가 실험 제안</strong><ul>${a.next_experiments.map(h => `<li>${esc(h)}</li>`).join('')}</ul>` : ''}</div>`;
    } else if (run.ai_error) html += `<div class="lab-ai">${esc(run.ai_error)}</div>`;
    el('lab-findings').innerHTML = html || `<div class="compact-empty"><span>◎</span><p>${running ? '측정이 끝나면 판정을 표시합니다.' : '판정할 측정값이 없습니다.'}</p></div>`;
    el('lab-timeline').innerHTML = run.events?.length
      ? run.events.slice().reverse().map(e => `<li><time>${esc(clock(e.time))}</time><div><b>${esc(stages[e.stage] || e.stage)}</b> ${esc(e.message)}</div></li>`).join('')
      : '<li class="timeline-placeholder">실험 시작을 기다리고 있습니다.</li>';
  }

  async function refresh() {
    try {
      runs = await request('/api/lab/runs');
      if (!selected && runs.length) selected = runs[0].id;
      renderHistory();
      if (selected) renderRun(await request(`/api/lab/runs/${selected}`));
    } catch (error) { showError('서버 연결을 확인해주세요. ' + error.message); }
  }
  async function poll() {
    clearTimeout(timer);
    if (!el('lab-workspace').hidden && !window.crashEvents.connected('lab')) await refresh();
    timer = setTimeout(poll, 4000);
  }

  el('lab-history').addEventListener('click', async event => {
    const button = event.target.closest('[data-lab-run]');
    if (button) { selected = button.dataset.labRun; await refresh(); }
  });
  el('lab-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || active(current)) return;
    const form = new FormData(el('lab-form'));
    const body = Object.fromEntries([...form.entries()].map(([k, v]) => [k, el('lab-form').elements[k].type === 'number' ? Number(v) : v]));
    busy = true; el('lab-start').disabled = true; showError('');
    try {
      const run = await request('/api/lab/runs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      selected = run.id; await refresh(); poll();
    } catch (error) { showError(error.message); }
    finally { busy = false; el('lab-start').disabled = !!active(current); }
  });
  el('lab-cancel').addEventListener('click', async () => {
    if (!current) return;
    el('lab-cancel').disabled = true;
    try { renderRun(await request(`/api/lab/runs/${current.id}/cancel`, {method:'POST'})); await refresh(); }
    catch (error) { showError(error.message); }
    finally { el('lab-cancel').disabled = false; }
  });

  window.crashLab = {refresh: poll};
  poll();
})();
