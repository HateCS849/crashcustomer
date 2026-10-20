(() => {
  const el = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const categoryNames = {openapi:'API 명세',test_plan:'실험 계획',reference:'참고 자료'};
  const size = bytes => bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes/1024).toFixed(1)} KB` : `${(bytes/1048576).toFixed(1)} MB`;
  let projects = [], project = null, selectedId = '', pending = [], uploading = false, editing = false, viewed = null;
  let selectionVersion = 0;
  const savedSelection = () => {try{return localStorage.getItem('crashcustomer-project') || '';}catch{return '';}};
  const saveSelection = id => {try{localStorage.setItem('crashcustomer-project',id);}catch{}};
  async function request(path, options={}) {
    const response = await fetch(path, options);
    if(response.status === 204) return null;
    const data = await response.json();
    if(!response.ok) {
      let message = data.detail;
      if(Array.isArray(message)) message = message.map(d=>({name:'프로젝트 이름',base_url:'API 서버 주소',browser_url:'브라우저 주소'}[d.loc?.at(-1)] || '입력값')+'을 확인해주세요.').join(' ');
      throw new Error(typeof message==='string'?message:'요청을 처리하지 못했습니다.');
    }
    return data;
  }
  function message(text, error=false) {
    const node=el('upload-message');node.textContent=text;node.hidden=!text;node.classList.toggle('error',error);
  }
  function updatePicker() {
    el('project-select').innerHTML='<option value="lab">로컬 실험실 · 장애 전파 시뮬레이터</option><option value="">내장 데모 · 행사 신청</option>'+projects.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} · 자료 ${p.attachment_count}개</option>`).join('');
    el('project-select').value=selectedId;
  }
  function resetPreview() {
    viewed=null;
    el('attachment-preview').innerHTML='<div class="compact-empty"><p>첨부된 파일을 선택해주세요.<small>명세는 API 목록, ZIP은 파일 목록을 확인할 수 있습니다.</small></p></div>';
  }
  async function selectProject(id) {
    if(uploading) return;
    const version=++selectionVersion;
    selectedId=id;project=null;pending=[];el('attachment-files').value='';renderPending();message('');resetPreview();saveSelection(id);
    el('project-select').value=id;
    el('project-workspace').hidden=!id || id==='lab';el('demo-workspace').hidden=!!id;el('lab-workspace').hidden=id!=='lab';
    if(id==='lab') window.crashLab?.refresh();
    if(!id || id==='lab') return;
    el('project-name').textContent='프로젝트를 불러오는 중…';el('project-description').textContent='';el('project-addresses').replaceChildren();el('attachment-list').replaceChildren();el('project-overview').replaceChildren();
    el('choose-files-button').disabled=true;el('edit-project-button').disabled=true;
    try {
      const result=await request(`/api/projects/${encodeURIComponent(id)}`);
      if(version!==selectionVersion) return;
      project=result;renderProject();
    } catch(error) {if(version===selectionVersion)message(error.message,true);}
  }
  function renderProject() {
    if(!project) return;
    el('choose-files-button').disabled=uploading;el('edit-project-button').disabled=uploading;
    el('project-name').textContent=project.name;
    el('project-description').textContent=project.description || 'API 명세와 실험 자료를 첨부해 이 프로젝트의 검증을 준비하세요.';
    el('project-addresses').innerHTML=[['API',project.base_url],['BROWSER',project.browser_url]].map(([label,url])=>`<span><b>${label}</b> ${url?`<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)} ↗</a>`:'주소 미등록'}</span>`).join('');
    const files=project.attachments;
    el('attachment-count').textContent=files.length;
    el('attachment-list').innerHTML=files.length?files.map(file=>`<article class="attachment-row ${viewed===file.id?'selected':''}"><button type="button" data-view="${esc(file.id)}" class="attachment-select"><span class="file-symbol">${file.category==='openapi'?'{}':file.format==='zip'?'▤':'≡'}</span><span><strong>${esc(file.filename)}</strong><small>${esc(categoryNames[file.category])} · ${size(file.size)}${file.category==='openapi'?` · API ${file.summary.operation_count}개`:file.format==='zip'?` · 파일 ${file.summary.file_count}개`:''}</small></span></button><a class="file-action" aria-label="${esc(file.filename)} 다운로드" href="/api/projects/${project.id}/attachments/${file.id}/download">↓</a><button type="button" class="file-action remove-file" data-remove="${file.id}" aria-label="${esc(file.filename)} 첨부 제거" ${uploading?'disabled':''}>×</button></article>`).join(''):'<div class="compact-empty"><span>▤</span><p>아직 첨부된 자료가 없습니다.<small>OpenAPI 파일부터 첨부해보세요.</small></p></div>';
    const specs=files.filter(f=>f.category==='openapi'),plans=files.filter(f=>f.category==='test_plan');
    el('project-overview').innerHTML=`<div><span>첨부 자료</span><strong>${files.length}<small> / 30</small></strong></div><div><span>명세에 등록된 API</span><strong>${specs.reduce((n,f)=>n+f.summary.operation_count,0)}<small>개</small></strong></div><ul><li class="${specs.length?'ready':''}">${specs.length?'✓':'○'} API 명세 ${specs.length?'등록됨':'미등록'}</li><li class="${plans.length?'ready':''}">${plans.length?'✓':'○'} 실험 계획 ${plans.length?'등록됨':'미등록'}</li><li class="${project.base_url?'ready':''}">${project.base_url?'✓':'○'} API 주소 ${project.base_url?'입력됨 · 연결 미검증':'미등록'}</li><li class="${project.execution?.status==='ready'?'ready':''}">${project.execution?.status==='ready'?'✓ 사전 점검 실행 가능':'○ 실행 차단 · 브라우저 주소 확인 필요'}</li></ul>`;
    window.crashCampaign?.load(project);
  }
  function renderPending() {
    el('pending-files').innerHTML=pending.map(f=>`<li><span>${esc(f.name)}</span><small>${size(f.size)}</small></li>`).join('');
    el('upload-button').disabled=uploading || !pending.length || !project;
    el('upload-button').firstElementChild.textContent=uploading?'자료를 첨부하는 중…':`선택한 파일 첨부${pending.length?' · '+pending.length+'개':''}`;
  }
  function chooseFiles(files) {
    if(uploading || !project)return;
    pending=Array.from(files);message('');
    if(pending.length+project.attachments.length>30){message('프로젝트당 30개까지 첨부할 수 있습니다.',true);pending=[];}
    else if(pending.some(f=>f.size>20*1024*1024)){message('파일 하나당 최대 20MB까지 첨부할 수 있습니다.',true);pending=[];}
    renderPending();
  }
  async function reloadProject() {
    project=await request(`/api/projects/${selectedId}`);
    projects=await request('/api/projects');updatePicker();renderProject();renderPending();
  }
  function openDialog(edit=false) {
    if(uploading)return;
    editing=edit;const p=edit?project:null;
    el('project-dialog-title').textContent=edit?'프로젝트 정보 수정':'프로젝트 등록';
    el('save-project-button').firstElementChild.textContent=edit?'변경 사항 저장':'프로젝트 만들기';
    el('project-input-name').value=p?.name || '';el('project-input-description').value=p?.description || '';
    el('project-input-api').value=p?.base_url || '';el('project-input-browser').value=p?.browser_url || '';
    el('project-form-error').hidden=true;el('project-dialog').showModal();
  }
  async function viewAttachment(id) {
    const requestedProject=selectedId,version=selectionVersion;
    try {
      const file=await request(`/api/projects/${requestedProject}/attachments/${id}/preview`);
      if(version!==selectionVersion)return;
      viewed=id;renderProject();
      let body=`<div class="preview-title"><span>${esc(categoryNames[file.category])} · ${esc(file.format.toUpperCase())}</span><h3>${esc(file.filename)}</h3><p>${esc(file.summary.notice)}</p></div>`;
      if(file.category==='openapi') {
        body+=`<div class="spec-heading"><strong>${esc(file.summary.title)}</strong><span>${file.summary.operation_count} ENDPOINTS</span></div><ul class="endpoint-list">${file.summary.operations.map(op=>`<li><span class="method method-${op.method.toLowerCase()}">${esc(op.method)}</span><div><code>${esc(op.path)}</code>${op.summary?`<small>${esc(op.summary)}</small>`:''}${op.authenticated?'<small class="auth-note">인증 필요</small>':''}${op.parameters.filter(p=>p.required).length?`<small>필수: ${esc(op.parameters.filter(p=>p.required).map(p=>p.name).join(', '))}</small>`:''}</div></li>`).join('')}</ul>`;
        if(file.summary.operation_count>file.summary.operations.length)body+='<p class="preview-footnote">최대 200개 API를 표시합니다.</p>';
      } else if(file.format==='zip') {
        body+=`<div class="spec-heading"><strong>파일 ${file.summary.file_count}개</strong><span>${size(file.summary.uncompressed_size)}</span></div><ul class="archive-list">${file.summary.files.map(f=>`<li><code>${esc(f.path)}</code><span>${size(f.size)}</span></li>`).join('')}</ul>`;
        if(file.summary.file_count>file.summary.files.length)body+='<p class="preview-footnote">최대 200개 파일을 표시합니다.</p>';
      }
      if(file.text!==null)body+=`<details class="raw-preview" ${file.category==='openapi'?'':'open'}><summary>첨부 원문 미리보기</summary><pre>${esc(file.text)}</pre><small>최대 12,000자 · 전체 내용은 다운로드로 확인하세요.</small></details>`;
      else if(file.format!=='zip')body+='<p class="preview-footnote">이 형식은 다운로드로 확인할 수 있습니다.</p>';
      el('attachment-preview').innerHTML=body;
    } catch(error){message(error.message,true);}
  }
  el('project-select').addEventListener('change',e=>selectProject(e.target.value));
  el('new-project-button').addEventListener('click',()=>openDialog());
  el('edit-project-button').addEventListener('click',()=>openDialog(true));
  el('close-project-dialog').addEventListener('click',()=>el('project-dialog').close());
  el('project-form').addEventListener('submit',async event=>{
    event.preventDefault();el('save-project-button').disabled=true;el('project-form-error').hidden=true;
    const payload={name:el('project-input-name').value,description:el('project-input-description').value,base_url:el('project-input-api').value,browser_url:el('project-input-browser').value};
    try {
      const result=await request(editing?`/api/projects/${selectedId}`:'/api/projects',{method:editing?'PATCH':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      projects=await request('/api/projects');selectedId=result.id;updatePicker();el('project-dialog').close();await selectProject(result.id);
    } catch(error){el('project-form-error').textContent=error.message;el('project-form-error').hidden=false;}
    finally{el('save-project-button').disabled=false;}
  });
  el('choose-files-button').addEventListener('click',()=>el('attachment-files').click());
  el('attachment-files').addEventListener('change',event=>chooseFiles(event.target.files));
  const zone=el('upload-dropzone');
  ['dragenter','dragover'].forEach(name=>zone.addEventListener(name,event=>{event.preventDefault();zone.classList.add('dragging');}));
  ['dragleave','drop'].forEach(name=>zone.addEventListener(name,event=>{event.preventDefault();zone.classList.remove('dragging');}));
  zone.addEventListener('drop',event=>chooseFiles(event.dataTransfer.files));
  el('attachment-form').addEventListener('submit',async event=>{
    event.preventDefault();if(uploading || !pending.length || !project)return;
    uploading=true;el('project-select').disabled=true;el('new-project-button').disabled=true;el('attachment-category').disabled=true;renderPending();renderProject();
    const category=el('attachment-category').value;
    let completed=0;const failures=[];const remaining=[];
    try {
      for(const file of pending) {
        message(`${completed+failures.length+1}/${pending.length} · ${file.name} 첨부 중…`);
        const body=new FormData();body.append('file',file);body.append('category',category);
        try{await request(`/api/projects/${selectedId}/attachments`,{method:'POST',body});completed++;}
        catch(error){failures.push(`${file.name}: ${error.message}`);remaining.push(file);}
      }
      pending=remaining;el('attachment-files').value='';await reloadProject();
      if(completed && project.attachments.length)await viewAttachment(project.attachments[0].id);
      message(`${completed}개 파일을 첨부했습니다.${failures.length?' '+failures.join(' / '):' 아래 목록에서 자료를 확인하세요.'}`,!!failures.length);
    } catch(error){message(error.message,true);}
    finally{uploading=false;el('project-select').disabled=false;el('new-project-button').disabled=false;el('attachment-category').disabled=false;renderProject();renderPending();}
  });
  el('attachment-list').addEventListener('click',async event=>{
    const view=event.target.closest('[data-view]'),remove=event.target.closest('[data-remove]');
    if(view){await viewAttachment(view.dataset.view);return;}
    if(remove && !uploading) {
      const id=remove.dataset.remove;remove.disabled=true;
      try{await request(`/api/projects/${selectedId}/attachments/${id}`,{method:'DELETE'});if(viewed===id)resetPreview();await reloadProject();message('첨부 사본을 제거했습니다. 원본 파일은 유지됩니다.');}
      catch(error){message(error.message,true);remove.disabled=false;}
    }
  });
  window.crashProjects={get selectedId(){return selectedId;},selectDemo:()=>selectProject('')};
  (async()=>{try{projects=await request('/api/projects');const saved=savedSelection();selectedId=(saved==='lab'||projects.some(p=>p.id===saved))?saved:'lab';updatePicker();await selectProject(selectedId);}catch(error){message(error.message,true);el('project-select').title='프로젝트 목록을 불러오지 못했습니다.';}})();
})();
