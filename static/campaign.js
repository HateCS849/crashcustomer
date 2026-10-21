(() => {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clock = s => new Date(s).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  const states = {queued:'대기',running:'점검 중',completed:'완료',error:'실행 오류',interrupted:'중단됨',partial:'부분 완료',inconclusive:'판정 불가'};
  const stages = {route:'의도 라우팅',chat_answer:'사용 안내',gate:'기준선 검증',replan_check:'누락 검사·재계획',write:'보고서 초안',validate:'결과 검증',finalize:'결과 확정',queued:'대기',recon:'탐색 에이전트',retrieve:'자료 검색(RAG)',plan:'계획 에이전트',attack:'장애 주입·검증',probe:'API 측정',summarize:'요약',complete:'완료',error:'오류'};
  const verification = {confirmed:'독립 재현 확인',unconfirmed:'재현 미확인',pending:'재현 대기',measured:'실측'};
  const active = c => c && ['queued','running'].includes(c.status);
  let project = null, campaigns = [], selected = null, current = null, busy = false, timer = null;

  async function request(path, options={}) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '요청을 처리하지 못했습니다.');
    return data;
  }
  function showError(text) { el('campaign-error').textContent = text; el('campaign-error').hidden = !text; }

  function renderConnection(state) {
    const ready = state.status === 'ready' && (!state.health || state.health.ok);
    el('connection-note').classList.toggle('ready', ready);
    el('connection-title').textContent = state.status !== 'ready' ? '이 주소는 실행하지 않습니다.' : state.health ? (state.health.ok ? '대상 앱이 응답합니다.' : '대상 앱이 응답하지 않습니다.') : '실행 가능한 테스트 주소입니다.';
    el('connection-message').textContent = state.health ? `${state.message} ${state.health.message}${state.health.latency_ms != null ? ` (${state.health.latency_ms}ms)` : ''}` : state.message;
    el('connection-badge').textContent = state.status !== 'ready' ? '실행 차단' : state.health ? (state.health.ok ? '연결됨' : '응답 없음') : '실행 가능';
    el('campaign-start').disabled = busy || state.status !== 'ready' || active(current);
  }

  function renderHistory() {
    el('campaign-history').innerHTML = campaigns.map(c => `<button type="button" class="lab-history-item ${selected===c.id?'selected':''}" data-campaign="${esc(c.id)}"><span><strong>${c.config.backend==='ollama'?'Ollama AI':'규칙 기반'} · 시나리오 ${c.summary ? c.summary.scenarios : '-'}개</strong><small>${esc(clock(c.created_at))} · 재현 결함 ${c.summary ? c.summary.confirmed : '-'}건</small></span><span class="run-state ${esc(c.status)}">${esc(states[c.status] || c.status)}</span></button>`).join('');
  }

  function renderCampaign(c) {
    current = c;
    window.crashEvents.watch('campaign', 'campaigns', c, renderCampaign);
    const running = active(c);
    el('campaign-start').disabled = busy || running || project?.execution?.status !== 'ready';
    el('campaign-start').firstElementChild.textContent = running ? '사전 점검 진행 중…' : '사전 점검 시작하기';
    el('campaign-state').textContent = `${states[c.status] || c.status}${running ? ' · ' + (stages[c.stage] || c.stage) : ''}`;
    el('campaign-state').className = `run-state ${c.status}`;
    el('campaign-report').hidden = running;
    el('campaign-report').href = `/api/campaigns/${c.id}/report`;
    el('campaign-elapsed').textContent = c.elapsed_seconds != null ? `${c.elapsed_seconds.toFixed(1)}초 · LLM ${c.summary?.llm_usage.calls ?? 0}회` : '실행 중 · 실제 기록';
    const s = c.summary;
    el('campaign-finding-count').textContent = s ? s.confirmed : 0;
    el('campaign-summary').innerHTML = s ? `<div class="campaign-stats"><div><span>장애 시나리오</span><strong>${s.scenarios}개</strong></div><div><span>재현·실측 확인 결함</span><strong class="${s.confirmed?'bad':''}">${s.confirmed} / ${s.findings}건</strong></div><div><span>위험 점수</span><strong class="${s.risk_score?'bad':''}">${s.risk_score}</strong></div><div><span>기준 실행 목표 완료</span><strong>${s.baseline_completed?'예':'아니오'}</strong></div></div>` : '';
    let html = c.validation ? `<div class="reference"><b>결과 검증 · ${esc(c.validation.verdict)}</b><p>${esc(c.validation.issues.join('\n') || '기준선·범위·재현 근거 검증 통과')}</p></div>` : '';
    if (!running && !c.validation && !c.answer) html += '<div class="error-result">검증기 추가 전의 이전 기록입니다. 현재 기준의 충분성 검증은 새 점검에서 확인하세요.</div>';
    if (c.answer) html += `<div class="reference">${esc(c.answer)}</div>`;
    if (c.coverage) html += `<div class="reference"><b>장애 유형 ${c.coverage.planned.length}/${c.coverage.available.length}개 · 계획 ${c.plan_attempts}회</b><p>미점검: ${esc([...c.coverage.missing, ...c.coverage.out_of_budget].join(', ') || '없음')}</p></div>`;
    if (c.mcp) html += `<details class="reference"><summary>MCP 실제 호출 ${c.mcp.calls.length}회</summary><p>${esc(c.mcp.calls.join('\n'))}</p></details>`;
    if (c.error) html += `<div class="error-result"><strong>사전 점검 실행 오류 · 서비스 결함 아님</strong><br>${esc(c.error)}</div>`;
    if (c.run_ids?.length) html += `<article class="scenario-row"><header><h3>기준 실행 · 장애 주입 없음</h3><span class="verification">관찰된 API ${c.observed_requests.length}개</span></header><p class="reason">${esc(c.observed_requests.map(r => r.method + ' ' + r.path).join(', ') || '관찰된 API 요청 없음')}</p><div class="evidence-links"><button type="button" data-run="${esc(c.run_ids[0])}">실행 상세 보기 ↗</button></div></article>`;
    html += (c.scenarios || []).map(sc => {
      const findings = sc.findings || [];
      const body = sc.status == null ? '<li class="ok">실행 대기 중</li>'
        : findings.length ? findings.map(f => `<li><b>${esc(f.title)}</b> · ${esc(verification[f.verification] || f.verification)}<br>${esc(f.actual)}</li>`).join('')
        : `<li class="ok">계약 위반 없음 · ${sc.result === 'goal_completed' ? '장애 후 재시도로 목표 완료' : esc(sc.result || sc.status)}</li>`;
      return `<article class="scenario-row"><header><h3>${esc(sc.label)}</h3>${sc.status ? `<span class="run-state ${esc(sc.status)}">${esc(sc.status === 'completed' ? '완료' : sc.status)}</span>` : ''}</header><p class="reason">계획 근거: ${esc(sc.reason)}</p><ul>${body}</ul>${sc.run_id ? `<div class="evidence-links"><button type="button" data-run="${esc(sc.run_id)}">화면·타임라인 보기 ↗</button><a href="/api/runs/${esc(sc.run_id)}/report">실행 보고서 ↓</a>${sc.trace_enabled === false ? '<span>인증 보호 · Trace 미저장</span>' : `<a href="/artifacts/${esc(sc.run_id)}/explore/trace.zip">Trace ↓</a>`}</div>` : ''}</article>`;
    }).join('');
    el('campaign-scenarios').innerHTML = html || '<div class="compact-empty"><span>⌁</span><p>기준 실행을 준비하고 있습니다.</p></div>';
    el('campaign-probes').innerHTML = renderProbes(c.probes, running);
    el('campaign-references').innerHTML = c.references?.length
      ? c.references.map(r => `<div class="reference"><b>${esc(r.source)} · ${esc(r.method)} ${esc(r.score)}</b><p>${esc(r.text.slice(0, 400))}</p></div>`).join('')
      : `<div class="compact-empty"><p>${running ? '검색 단계 이후 표시합니다.' : '참고한 첨부 자료가 없습니다. OpenAPI 명세나 운영 문서를 첨부하면 계획 근거로 사용합니다.'}</p></div>`;
    el('campaign-timeline').innerHTML = c.events?.length
      ? [...c.events, ...(c.active_run?.events || []).map(e => ({...e, stage:'탐색 · ' + e.node}))].sort((a,b) => a.time.localeCompare(b.time)).reverse().map(e => `<li><time>${esc(clock(e.time))}</time><div><b>${esc(stages[e.stage] || e.stage)}</b> ${esc(e.message)}</div></li>`).join('')
      : '<li class="timeline-placeholder">점검 시작을 기다리고 있습니다.</li>';
  }

  function renderProbes(probes, running) {
    if (!probes) return `<div class="compact-empty"><p>${running ? '장애 시나리오가 끝난 뒤 측정합니다.' : 'API 측정을 실행하지 않았습니다.'}</p></div>`;
    let html = probes.findings.map(f => `<div class="lab-finding warning"><i>!</i><div><b>${esc(f.title)}</b>${esc(f.actual)}</div></div>`).join('');
    const v = probes.validation;
    if (v) {
      const bad = v.results.filter(r => r.status >= 500 || r.status === 0);
      html += `<div class="reference"><b>입력 검증 · 잘못된 요청 ${v.requests}건</b><p>${bad.length ? bad.slice(0, 6).map(r => `${r.method} ${r.path} · ${r.variant} → ${r.status}`).join('\n') : v.valid === false ? '인증 실패 · 입력 검증 무효' : v.accepted_invalid ? `잘못된 입력 ${v.accepted_invalid}건이 2xx로 수락됨 · 추가 확인 필요` : '관찰된 잘못된 요청은 4xx로 거절되었습니다.'}</p></div>`;
    }
    const load = probes.load;
    if (load && load.valid === false) html += `<div class="error-result"><b>부하 측정 무효</b><p>${esc(load.reason)}</p>${esc((load.preflight || []).map(p => `${p.path} → ${p.status}`).join(', '))}</div>`;
    if (load && load.valid !== false) {
      html += `<div class="lab-case"><p>단계별 부하 · ${esc(load.paths.join(', '))} · 목표 P95 ${load.slo_ms}ms · ${load.saturation_users ? `동시 ${load.saturation_users}명에서 목표 초과` : '목표 이내'}</p><div class="lab-table-wrap"><table class="lab-table"><thead><tr><th>동시 사용자</th><th>요청</th><th>오류율</th><th>P50(ms)</th><th>P95(ms)</th><th>처리량(rps)</th></tr></thead><tbody>${load.stages.map(st => `<tr><td>${st.users}명</td><td>${st.requests}</td><td class="${st.error_rate > .05 ? 'bad' : ''}">${(st.error_rate * 100).toFixed(1)}%</td><td>${st.p50_ms}</td><td class="${st.p95_ms > load.slo_ms ? 'bad' : ''}">${st.p95_ms}</td><td>${st.throughput_rps}</td></tr>`).join('')}</tbody></table></div></div>`;
    }
    return html || '<div class="compact-empty"><p>측정할 요청이 없었습니다.</p></div>';
  }

  function resetResults() {
    current = null; window.crashEvents.watch('campaign', 'campaigns', null, renderCampaign);
    el('campaign-summary').innerHTML = '';
    el('campaign-finding-count').textContent = 0;
    el('campaign-report').hidden = true;
    el('campaign-state').textContent = '대기 중'; el('campaign-state').className = 'run-state';
    el('campaign-scenarios').innerHTML = '<div class="compact-empty"><span>⌁</span><p>사전 점검을 시작하면 시나리오별 결함과 재현 결과를 표시합니다.<small>로컬·허용 호스트에서만 실행하며, 같은 출처 API에만 장애를 주입합니다.</small></p></div>';
    el('campaign-probes').innerHTML = '<div class="compact-empty"><p>장애 시나리오가 끝난 뒤 관찰된 요청으로 측정합니다.</p></div>';
    el('campaign-references').innerHTML = '<div class="compact-empty"><p>점검을 실행하면 계획 에이전트가 참고한 첨부 자료 조각을 표시합니다.</p></div>';
    el('campaign-timeline').innerHTML = '<li class="timeline-placeholder">탐색 → 검색 → 계획 → 장애 주입 → API 측정 → 요약 순서로 진행합니다.</li>';
  }

  async function refresh() {
    if (!project) return;
    const id = project.id;
    try {
      campaigns = await request(`/api/projects/${id}/campaigns`);
      if (project?.id !== id) return;
      if (!selected && campaigns.length) selected = campaigns[0].id;
      renderHistory();
      if (selected) renderCampaign(await request(`/api/campaigns/${selected}`));
    } catch (error) { showError(error.message); }
  }
  async function poll() {
    clearTimeout(timer);
    if (project && !el('project-workspace').hidden && !window.crashEvents.connected('campaign')) await refresh();
    timer = setTimeout(poll, 5000);
  }
  async function checkConnection(withHealth) {
    if (!project) return;
    renderConnection(project.execution);
    if (!withHealth || project.execution.status !== 'ready') return;
    el('check-connection').disabled = true;
    try { renderConnection(await request(`/api/projects/${project.id}/connection`)); }
    catch (error) { showError(error.message); }
    finally { el('check-connection').disabled = false; }
  }

  el('check-connection').addEventListener('click', () => checkConnection(true));
  el('campaign-history').addEventListener('click', async event => {
    const button = event.target.closest('[data-campaign]');
    if (button) { selected = button.dataset.campaign; await refresh(); }
  });
  el('campaign-scenarios').addEventListener('click', async event => {
    const button = event.target.closest('[data-run]');
    if (button) { window.crashProjects?.selectDemo(); await window.crashRuns?.show(button.dataset.run); window.scrollTo({top: 0, behavior: 'smooth'}); }
  });
  el('campaign-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || active(current) || !project) return;
    busy = true; el('campaign-start').disabled = true; showError('');
    try {
      const result = await request(`/api/projects/${project.id}/campaigns`, {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({goal: el('campaign-goal').value, backend: el('campaign-backend').value, max_scenarios: Number(el('campaign-max').value),
          api_checks: el('campaign-api').checked, load_test: el('campaign-load').checked, load_max_users: Number(el('campaign-users').value), test_token: el('campaign-token').value || null})});
      el('campaign-token').value = '';
      selected = result.id; await refresh(); poll();
    } catch (error) { showError(error.message); }
    finally { busy = false; if (current) renderCampaign(current); else el('campaign-start').disabled = project?.execution?.status !== 'ready'; }
  });

  window.crashCampaign = {
    load(next) {
      const changed = project?.id !== next.id;
      project = next;
      if (changed) { el('campaign-token').value = ''; selected = null; campaigns = []; el('campaign-history').innerHTML = ''; showError(''); resetResults(); }
      checkConnection(changed);
      poll();
    },
  };
})();
