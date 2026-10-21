/* Native EventSource reconnects with Last-Event-ID; snapshots replace state atomically. */
(() => {
  const streams = new Map();
  const lastIds = new Map();
  window.crashEvents = {
    watch(key, kind, record, update) {
      const previous = streams.get(key);
      const active = record && ['queued','running','replaying'].includes(record.status);
      const url = active ? `/api/events/${kind}/${encodeURIComponent(record.id)}` : null;
      if (previous?.url === url) return;
      previous?.source.close(); streams.delete(key);
      if (!url) return;
      const source = new EventSource(url);
      streams.set(key, {url, source});
      source.addEventListener('update', event => {
        if (streams.get(key)?.source !== source || lastIds.get(url) === event.lastEventId) return;
        lastIds.set(url, event.lastEventId);
        update(JSON.parse(event.data));
      });
      source.addEventListener('done', () => { source.close(); if (streams.get(key)?.source === source) streams.delete(key); });
      source.onerror = () => {
        const badge = document.getElementById('stream-status');
        if (badge) badge.textContent = 'SSE 재연결 중';
      };
      source.onopen = () => {
        const badge = document.getElementById('stream-status');
        if (badge) badge.textContent = 'SSE 연결됨 · 단계별 실시간 기록';
      };
    },
    connected(key) { return streams.has(key); },
  };
  window.addEventListener('pagehide', () => { for (const s of streams.values()) s.source.close(); });
  const answer = document.getElementById('assistant-answer');
  document.getElementById('assistant-route').addEventListener('click', async () => {
    try {
      const response = await fetch('/api/assistant/route', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:document.getElementById('assistant-message').value})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail);
      answer.textContent = result.answer;
      if (result.intent === 'lab') {
        const picker = document.getElementById('project-select');
        picker.value = 'lab'; picker.dispatchEvent(new Event('change'));
      } else if (result.intent === 'campaign') document.getElementById('campaign-goal').value = document.getElementById('assistant-message').value;
    } catch (error) { answer.textContent = error.message; }
  });
  async function traceStatus() {
    try {
      const status = await (await fetch('/api/tracing/status')).json();
      document.getElementById('tracing-status').textContent = `LangSmith ${status.enabled ? '활성 설정' : '설정 안 됨'} · 수신 검증: ${status.delivery?.status || '미실행'}`;
    } catch (_) { document.getElementById('tracing-status').textContent = '추적 설정을 확인하지 못했습니다.'; }
  }
  document.getElementById('verify-tracing').addEventListener('click', async event => {
    event.target.disabled = true;
    document.getElementById('tracing-status').textContent = '합성 추적 전송·수신 확인 중…';
    try {
      const result = await (await fetch('/api/tracing/verify', {method:'POST'})).json();
      document.getElementById('tracing-status').textContent = `수신 검증: ${result.status} · ${result.message || result.error_type || result.run_id || ''}`;
    } catch (_) { document.getElementById('tracing-status').textContent = '수신 검증 요청을 완료하지 못했습니다.'; }
    finally { event.target.disabled = false; }
  });
  traceStatus();
})();
