'use strict';
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const modes = {cloudyfan:'CloudyFan MRC',fixed_high:'全大模型優先',low_first:'全便宜模型優先',open_source_first:'全開源模型優先'};
const tiers = {open_source:'開源池',low:'低分池',mid:'中分池',high:'高分池'};
const candidateTiers = {open_source:'開源候選',low:'低分候選',mid:'中分候選',high:'高分候選'};
const labels = {overview:'運作總覽',tasks:'任務與升級',ledger:'費用帳本',billing:'客戶帳務',pools:'候選池與契約',capacity:'供應商限額來源',queue:'排隊與等待',observations:'建立時觀測規則',clients:'連線客戶端'};
const statuses = {passed:['通過','green'],failed:['未通過','red'],unverifiable:['無法確認','amber'],running:['執行中','blue'],ready:['已準備','blue'],bound:['已凍結','blue'],queued:['排隊中','blue'],expired:['已到期','amber'],task_busy:['執行中','blue'],engineering_interrupted:['工程中斷','red'],accounting_interrupted:['費用待核','amber'],policy_denied:['執行條件不符','amber']};
const poolStatuses = {missing:'缺額',pending_unqualified:'待驗證',pending_unqualified_stale_source:'來源逾期',research_admitted_only:'僅舊研究准入',mock_only:'模擬設定'};
const categoryLabels = {'agentic_index':'代理任務','coding_index':'程式與 SQL','intelligence_index':'綜合推理','3d':'3D 設計','asciiart':'字元藝術','audiorealism':'音訊擬真','codecategories':'程式設計','dataviz':'資料視覺化','gamedev':'遊戲開發','graphicdesign':'平面設計','image':'影像生成','imageediting':'影像編輯','logo':'Logo 設計','svg':'SVG 圖形','text-to-speech':'語音合成','uicomponent':'UI 元件','website':'網站設計','gpqa_diamond':'科學推理','search_browsecomp':'網頁搜尋','search_dsqa':'搜尋問答','search_hle':'高難度搜尋','search_widesearch':'廣域搜尋','tau_bench_verified_airline':'航空客服代理'};
const errors = {unauthorized:'登入憑證不正確或已失效',configuration_changed_reload:'設定已被更新，請重新讀取後再套用',source_get_failed:'來源 API 暫時無法取得資料',source_timeout:'來源連線逾時',source_request_deadline:'單次來源請求逾時',source_network_failed:'來源連線失敗',source_schema_invalid:'來源資料格式或模型身分不符',source_request_limit:'本次來源查詢已達上限',source_refresh_deadline:'來源更新逾時，現有版本保留',source_refresh_busy:'另一個來源更新仍在進行',monitor_or_source_unavailable:'監控服務或資料來源暫時無法使用',candidate_data_has_no_registered_runtime_contracts:'此候選版本尚未接入核心執行設定',routing_configuration_unavailable:'此類別與模式尚無已註冊契約',service_busy:'執行容量已滿，請稍後再試',task_expired:'任務已到期，原結果與接續已停止開放'};
let token = '', data = null, pool = null, selectedCategory = 'artificial-analysis:coding_index', selectedClient = '', taskFilter = '', modeFilter = '', search = '', pendingConfirm = null, busy = false, refreshCompleteness = null;
const expandedTaskTraces = new Set();
const expandedCandidateDetails = new Set();
let candidateRead = {status:'idle'}, candidateRequest = null;
let simulationStarting = false;
let authenticationVersion = 0, snapshotFlight = null, operatorSession = null;
let confirmationVersion = 0, toastVersion = 0;
let selectedCandidateVersion = '', selectedCandidateMode = 'cloudyfan', candidateFlight = 0;
function setToken(value) {
 token=value;authenticationVersion+=1;operatorSession=null;data=null;pool=null;
 selectedCandidateVersion='';selectedCandidateMode='cloudyfan';selectedClient='';taskFilter='';modeFilter='';search='';
 candidateFlight+=1;refreshCompleteness=null;pendingConfirm=null;confirmationVersion+=1;toastVersion+=1;busy=false;simulationStarting=false;
 expandedTaskTraces.clear();expandedCandidateDetails.clear();candidateRead={status:'idle'};candidateRequest=null;
 window.CloudyFanBilling?.resetView();window.CloudyFanCapacity?.resetView();window.CloudyFanQueue?.resetView();window.CloudyFanObservationPolicy?.resetView();
 $('#confirm').close();$('#apply-confirm').disabled=false;$('#error').hidden=true;$('#error').textContent='';$('#toast').hidden=true;
 $('#updated').textContent='尚未讀取此身分的資料';$('#environment').textContent='操作員身分尚未確認';$('#environment').className='badge';
 $('#main').innerHTML=`<div class="empty">${value?'正在核對操作員身分…':'已清除登入；請重新認證。'}</div>`;
 $('#token').value='';$('#login-error').textContent='';
}
const sameLogin = (version,value) => authenticationVersion===version&&token===value&&!!token;
function staleAuthentication() {const failure=Error('登入身分已變更，忽略原回應。');failure.stale=true;return failure;}
const money = v => v === null || v === undefined ? '未知' : '$' + Number(v).toFixed(6);
const pct = v => v === null || v === undefined ? '—' : (v * 100).toFixed(1) + '%';
const secs = v => v === null || v === undefined ? '—' : Number(v).toFixed(2) + 's';
const when = v => v ? new Date(typeof v === 'number' ? v*1000 : v).toLocaleString('zh-TW',{hour12:false}) : '尚未觀測';
const cname = c => categoryLabels[c?.split(':').at(-1)] || c;
const badge = (text, color = '') => `<span class="badge ${color}">${esc(text)}</span>`;
const status = s => {const v = statuses[s] || [s,'']; return badge(v[0],v[1]);};
const options = (values, selected, inherit = false, enabled = null) => (inherit ? '<option value="">跟隨全域</option>' : '') + Object.entries(values).map(([k,v])=>`<option value="${esc(k)}" ${k===selected?'selected':''} ${enabled&&!enabled.includes(k)?'disabled':''}>${esc(v)}${enabled&&!enabled.includes(k)?' · 無已註冊契約':''}</option>`).join('');
function error(message) {$('#error').hidden=false;$('#error').textContent=message;}
function toast(message) {const version=authenticationVersion,notice=++toastVersion;$('#toast').hidden=false;$('#toast').textContent=message;setTimeout(()=>{if(version===authenticationVersion&&notice===toastVersion)$('#toast').hidden=true;},4500);}
async function api(path, body) {
  const requestToken=token,requestVersion=authenticationVersion;
  if(!requestToken||(!operatorSession&&path!=='/monitor/session'))throw staleAuthentication();
  const response = await fetch(path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+requestToken,...(body===undefined?{}:{'Content-Type':'application/json'})},...(body===undefined?{}:{body:JSON.stringify(body)})});
  const result = await response.json();
  if(!sameLogin(requestVersion,requestToken))throw staleAuthentication();
  if(!response.ok) {
    if(response.status===401&&operatorSession){setToken('');if(!$('#login').open)$('#login').showModal();$('#login-error').textContent=errors.unauthorized;}
    const failure=Error(errors[result.error?.code] || result.error?.code || '無法取得資料');
    failure.completeness=result.completeness;failure.status=response.status;failure.code=result.error?.code;throw failure;
  }
  return result;
}
function confirmAction(title, description, action, successMessage = '設定已保存；既有任務仍使用原設定。', refreshHistory = true) {
  if(!operatorSession)return;
  $('#confirm-title').textContent=title;$('#confirm-description').innerHTML=description;
  pendingConfirm={action,successMessage,refreshHistory,version:++confirmationVersion,loginVersion:authenticationVersion,token};$('#apply-confirm').disabled=false;$('#confirm').showModal();
}
$('#cancel-confirm').onclick=()=>{$('#confirm').close();pendingConfirm=null;confirmationVersion+=1;};
$('#apply-confirm').onclick=async()=>{
  if(!pendingConfirm)return;$('#apply-confirm').disabled=true;
  const intent=pendingConfirm,current=()=>intent.version===confirmationVersion&&sameLogin(intent.loginVersion,intent.token)&&!!operatorSession;
  try{await intent.action();if(!current())return;$('#confirm').close();pendingConfirm=null;if(intent.refreshHistory)await load(true);if(current())toast(intent.successMessage);}
  catch(e){if(current()){error(e.message);$('#confirm').close();pendingConfirm=null;}}
  finally{if(current())$('#apply-confirm').disabled=false;}
};
function sessionContext() {
 const version=authenticationVersion,value=token,current=()=>sameLogin(version,value)&&!!operatorSession;
 return {api:(...args)=>current()?api(...args):Promise.reject(staleAuthentication()),esc,
   toast:message=>{if(current())toast(message);},error:message=>{if(current())error(message);},
   confirmAction:(...args)=>{if(current())confirmAction(...args);},demo:operatorSession.demo,sessionVersion:version};
}
async function authenticate(value) {
 setToken(value);const version=authenticationVersion,requestToken=token;
 try{
   const session=await api('/monitor/session');
   if(!sameLogin(version,requestToken))return {ok:false,stale:true};
   if(session?.schema_version!=='monitor-operator-session-v1'||session.authenticated!==true||typeof session.demo!=='boolean'||Object.keys(session).length!==3)throw Error('操作員身分確認資料無法核對。');
   operatorSession=session;render();return {ok:true};
 }catch(e){
   if(!sameLogin(version,requestToken))return {ok:false,stale:true};
   setToken('');return {ok:false,error:e};
 }
}
function heading(eyebrow,title,text,action='') {return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${text}</p></div>${action}</div>`;}
function metric(label,value,note,accent=false) {return `<article class="metric"><div class="metric-label">${label}</div><div class="metric-value ${accent?'accent':''}">${value}</div><div class="sub">${note}</div></article>`;}
function route(t) {return `<div class="route">${(t.path||[]).map((p,i)=>`${i?'<span class="arrow">→</span>':''}<span class="route-node ${p.passed===true?'passed':p.passed===false?'failed':''}" title="${esc((p.models||[]).join(', '))}">${esc(tiers[p.tier]||p.tier)}</span>`).join('') || '<span class="muted">路徑證據尚未完成</span>'}</div>`;}
function modeControls(client='', value=data.control.mode) {
 const available=data.available_modes||Object.keys(modes);
 return `<div class="mode-control"><label class="mode-label" for="mode-${esc(client||'global')}">${client?'此客戶端模式':'新任務預設模式'}</label><select id="mode-${esc(client||'global')}" data-mode-client="${esc(client)}" aria-label="${client?'客戶端':'全域'}模式">${options(modes,value,!!client,available)}</select><button class="button small" data-change-mode="${esc(client)}" ${!client&&!available.includes(value)?'disabled':''}>套用</button><span class="sub">各類別支援範圍見契約矩陣</span></div>`;
}
const taskTraceKey = task => JSON.stringify([task.client ?? '',task.run ?? '']);
const taskTraceId = task => 'task-trace-' + encodeURIComponent(taskTraceKey(task));
const taskMoney = value => typeof value === 'string' ? (/^\d+(?:\.\d+)?$/.test(value) ? '$' + esc(value) : '未知') : money(value);
const taskTrace = task => window.CloudyFanTrace?.render(task) || '<p class="trace-empty">呼叫與選池明細尚未載入。</p>';
function taskRows(tasks,compact=false) {
 return `<div class="table-wrap"><table><thead><tr><th>任務 / 客戶端</th><th>模式</th><th>執行路徑</th><th>結果</th>${compact?'':'<th>Jev 次數</th><th>完整 API</th><th>核心流程延遲</th>'}</tr></thead><tbody>${tasks.slice(0,200).map(t=>{
   const key=taskTraceKey(t),open=expandedTaskTraces.has(key),detailId=taskTraceId(t);
   return `<tr data-task-run="${esc(t.run)}"><td><span class="mono">${esc(t.run)}</span><br><span class="muted">${esc(t.client)}</span>${compact?'':`<br><button class="button small task-trace-toggle" type="button" data-task-trace-toggle="${esc(key)}" aria-expanded="${open}" aria-controls="${esc(detailId)}" aria-label="${open?'收起':'查看'}任務 ${esc(t.run)} 的呼叫與選池明細">${open?'收起明細':'查看呼叫與選池'}</button>`}</td><td>${esc(modes[t.mode])}</td><td>${route(t)}</td><td>${status(t.status)}${t.access_expired||t.status==='expired'?'<br><span class="muted">歷史統計保留</span>':''}</td>${compact?'':`<td class="mono">${esc(t.classifier_calls??'—')}</td><td class="amount">${taskMoney(t.complete_api_usd)}</td><td class="mono">${secs(t.wall_seconds)}</td>`}</tr>${compact?'':`<tr id="${esc(detailId)}" class="task-trace-row" data-task-trace-row="${esc(key)}" ${open?'':'hidden'}><td colspan="7">${open?taskTrace(t):''}</td></tr>`}`;
 }).join('')||`<tr><td colspan="${compact?4:7}" class="empty">尚無符合條件的任務</td></tr>`}</tbody></table></div>`;
}
function bindTaskTraces() {
 document.querySelectorAll('[data-task-trace-toggle]').forEach(button=>button.onclick=()=>{
   const key=button.dataset.taskTraceToggle,detail=document.getElementById(button.getAttribute('aria-controls'));
   const task=data.tasks.find(item=>taskTraceKey(item)===key);
   if(!detail||!task)return;
   const open=!expandedTaskTraces.has(key);
   if(open){expandedTaskTraces.add(key);detail.firstElementChild.innerHTML=taskTrace(task);}
   else expandedTaskTraces.delete(key);
   detail.hidden=!open;button.setAttribute('aria-expanded',String(open));
   button.setAttribute('aria-label',`${open?'收起':'查看'}任務 ${task.run} 的呼叫與選池明細`);
   button.textContent=open?'收起明細':'查看呼叫與選池';
 });
}
function simulationBanner() {
 const simulation=data.simulation;
 if(!simulation||typeof simulation!=='object')return '';
 const labels={idle:['尚未開始','blue'],running:['執行中','blue'],completed:['模擬已完成','green'],failed:['模擬中斷','red']};
 const current=Object.hasOwn(labels,simulation.status)?labels[simulation.status]:['狀態未知','amber'];
 const total=Number.isSafeInteger(simulation.total)&&simulation.total>0?simulation.total:null;
 const completed=Number.isSafeInteger(simulation.completed)&&simulation.completed>=0&&(total===null||simulation.completed<=total)?simulation.completed:null;
 const canStart=simulation.can_start===true&&simulation.status!=='running'&&!simulationStarting&&total===50;
 return `<section id="simulation-banner" class="simulation-banner" aria-labelledby="simulation-title"><div class="simulation-banner-heading"><div><div class="eyebrow">OLLAMA / SYNTHETIC BATCH</div><h2 id="simulation-title">Ollama 客戶端 · 50 題模擬</h2><p>透過 Ollama SDK 與本機 HTTP 入口，使用合成模型回應及合成帳務觀察路由。真實模型 API 費用為 $0。</p></div>${badge(...current)}</div><div class="simulation-progress" role="status"><span id="simulation-progress-label" class="simulation-progress-label">已完成 ${esc(completed??'未知')} / ${esc(total??'未知')} 題</span>${total===null?'':`<progress id="simulation-progress" max="${total}" ${completed===null?'':`value="${completed}"`} aria-label="模擬完成進度"></progress>`}</div><p class="simulation-identity metadata">客戶端 <code>${esc(simulation.customer??'未知')}</code> · 模型別名 <code>${esc(simulation.alias??'未知')}</code><br>批次 <code id="simulation-batch">${esc(simulation.batch_id??'未知')}</code></p><div class="simulation-actions"><button id="simulation-start" class="button primary" type="button" ${canStart?'':'disabled'}>${simulationStarting?'正在建立模擬批次…':simulation.status==='running'?'模擬執行中':'執行 50 題模擬'}</button><a id="simulation-view-tasks" class="button" href="#tasks">查看此客戶端任務</a><span class="metadata muted">進度隨帳務與任務快照更新；需確認才會建立批次。</span></div></section>`;
}
function bindSimulation() {
 if($('#simulation-start'))$('#simulation-start').onclick=()=>{
   const simulation=data.simulation;
   if(simulationStarting||simulation?.can_start!==true||simulation.status==='running'||simulation.total!==50)return;
   confirmAction('開始 Ollama 50 題模擬',`<p>以客戶端 <strong>${esc(simulation.customer??'未知')}</strong>，模型別名 <code>${esc(simulation.alias??'未知')}</code> 建立一個 50 題模擬批次。</p><p>使用 Ollama SDK 與本機 HTTP 接線，模型回應與帳務均為合成資料；真實模型 API 費用為 $0。各題的作答、Jev 選池及費用紀錄會出現在 Monitor。</p>`,async()=>{
     if(simulationStarting)throw Error('模擬批次正在建立，請等候原結果。');
     const version=authenticationVersion,value=token,current=()=>sameLogin(version,value)&&!!operatorSession;
     simulationStarting=true;render();
     try{
       const result=await api('/monitor/simulation/start',{}),next=result.simulation||result;
       if(current()&&data&&next&&['idle','running','completed','failed'].includes(next.status))data.simulation={...data.simulation,...next};
     }finally{if(current()){simulationStarting=false;render();}}
   },'已送出 50 題模擬批次；進度會持續更新。');
 };
 if($('#simulation-view-tasks'))$('#simulation-view-tasks').onclick=()=>{
   if(typeof data.simulation?.customer==='string'){taskFilter=data.simulation.customer;modeFilter='';search='';}
 };
}
function overview() {
 const s=data.summary, l=data.ledger, m=data.by_mode.cloudyfan;
 return heading('OPERATIONS / LIVE','路由運作，一眼掌握。','從首答到升級，讓每次選模與完整費用都能追查。')+
 simulationBanner()+
 `<div class="mode-banner"><div><div class="eyebrow">ACTIVE ROUTING</div><h3>${esc(modes[data.control.mode])}</h3><p>模式與模型池在任務建立時凍結；切換僅影響之後的新任務。</p></div>${modeControls()}</div>`+
 `<div class="metrics">${metric('最終公開驗收通過',pct(s.final_rate),`${s.passed} / ${s.tasks} 件；未知與未完成仍在分母`,true)}${metric('完整 API 費用',money(l.total_usd),`已知 ${money(l.known_usd)} · 待核 ${l.unknown_events} 筆`)}${metric('首答通過',pct(s.first_rate),`${s.first_passed} 件首答完成 · ${s.escalated} 件進入補答`)}${metric('核心流程延遲 P95',secs(s.latency_p95),`P50 ${secs(s.latency_p50)} · ${s.latency_samples} 件有完整證據`)}</div>`+
 `<div class="grid"><section class="panel"><div class="panel-header"><h2>CloudyFan 的選模路徑</h2>${badge('僅 MRC 任務')}</div><div class="panel-body"><div class="route-row"><div><h3>首答完成</h3><p>公開驗收通過後立即結束</p></div><strong class="mono">${m.first_passed} / ${m.tasks}</strong></div><div class="route-row"><div><h3>呼叫一次 Jev</h3><p>包含成功與最終未通過的完整流程</p></div><strong class="mono">${m.jev_once} / ${m.tasks} · ${pct(m.tasks ? m.jev_once/m.tasks : null)}</strong></div><div class="route-row"><div><h3>呼叫兩次 Jev</h3><p>第二次使用最近一次作答的公開診斷</p></div><strong class="mono">${m.jev_twice} / ${m.tasks} · ${pct(m.tasks ? m.jev_twice/m.tasks : null)}</strong></div><div class="route-row"><div><h3>需要關注</h3><p>未通過 ${m.failed} · 無法確認 / 中斷 ${m.unknown}</p></div>${badge(`${m.in_progress} 件進行中`,'blue')}</div></div></section>
 <section class="panel"><div class="panel-header"><h2>客戶端活動</h2><a href="#clients" class="muted">查看全部 →</a></div><div class="panel-body">${data.clients.map(c=>`<div class="route-row"><div><h3>${esc(c.label)}</h3><p>${esc(modes[c.effective_mode])}</p></div><div>${badge(c.recently_seen?'90 秒內有活動':'目前閒置',c.recently_seen?'green':'')}<p class="mono">${c.stats.tasks} tasks</p></div></div>`).join('')}<p class="mt">依認證請求的最近活動時間判斷，不冒充 TCP 連線數。</p></div></section></div>
 <section class="panel mt"><div class="panel-header"><h2>各模式運作統計</h2>${badge('不同任務分布，非配對降本實驗')}</div><div class="table-wrap"><table><thead><tr><th>模式</th><th>任務</th><th>首答通過</th><th>最終通過</th><th>補答</th><th>完整 API</th><th>流程 P95</th></tr></thead><tbody>${Object.entries(data.by_mode).map(([mode,v])=>`<tr><td>${esc(modes[mode])}</td><td>${v.tasks}</td><td>${pct(v.first_rate)}</td><td>${pct(v.final_rate)}</td><td>${v.escalated}</td><td class="amount">${money(v.complete_api_usd)}</td><td>${secs(v.latency_p95)}</td></tr>`).join('')}</tbody></table></div><div class="table-note">公開驗收結果；流程延遲不含外層佇列與客戶端網路。不同模式的任務不一定相同，因此不自動宣稱降本率。</div></section>
 <section class="panel mt"><div class="panel-header"><h2>最近任務</h2><a href="#tasks" class="muted">完整路由紀錄 →</a></div>${taskRows(data.tasks.slice(0,6),true)}</section>`;
}
function tasksPage() {
 let tasks=data.tasks.filter(t=>(!taskFilter||t.client===taskFilter)&&(!modeFilter||t.mode===modeFilter)&&(!search||[t.run,t.client,t.category].join(' ').toLowerCase().includes(search.toLowerCase())));
 return heading('REQUESTS / TRACE','任務與失敗升級','查看每件任務的凍結模式、作答路徑、分類次數與完整費用。')+
 `<div class="notice">任務結果與接續自建立起開放 ${esc(data.retention_policy?.task_access_days??30)} 天；到期顯示「已到期」，既有費用與歷史統計仍保留。服務同時最多執行一件模型任務，滿載不暗中排隊。</div>`+
 `<div class="filters"><input id="task-search" placeholder="搜尋任務、客戶端或分類" value="${esc(search)}" aria-label="搜尋任務"><select id="task-client" aria-label="依客戶端篩選"><option value="">所有客戶端</option>${options(Object.fromEntries(data.clients.map(c=>[c.id,c.label])),taskFilter)}</select><select id="task-mode" aria-label="依模式篩選"><option value="">所有模式</option>${options(modes,modeFilter)}</select><span class="muted">${tasks.length} 件</span></div><section class="panel">${taskRows(tasks)}<div class="table-note">顯示最近 200 件。核心流程延遲缺證維持「—」；公開驗收不是獨立隱藏判分。</div></section>`;
}
function attribution(ledger) {
 if(!ledger.by_client_known_usd)return '';
 const names=Object.fromEntries(data.clients.map(client=>[client.id,client.label]));
 return `<section class="panel mt"><div class="panel-header"><h2>費用歸屬</h2>${badge('付款帳戶共用總預算')}</div><div class="table-wrap"><table><thead><tr><th>歸屬</th><th>已知費用</th></tr></thead><tbody>${Object.entries(ledger.by_client_known_usd).map(([client,cost])=>`<tr><td>${esc(names[client]||client)}</td><td class="amount">${money(cost)}</td></tr>`).join('')}<tr><td>帳戶歷史／未歸屬費用</td><td class="amount">${money(ledger.unattributed_known_usd)}</td></tr></tbody></table></div><div class="table-note">客戶端只列可由任務證據歸屬的費用；未經此入口的歷史支出仍保留在總帳，不猜測分攤。未知金額另列待核。</div></section>`;
}
function ledgerPage() {
 const l=data.ledger;
 return heading('ACCOUNTING / EVIDENCE','每一筆費用，都留下來。','包含分類、生成、失敗、修正及中斷；未知費用不會被算成零。')+
 `<div class="metrics">${metric('已知累計',money(l.known_usd),'包含未完成流程，不只加總成功任務',true)}${metric('完整總額',money(l.total_usd),l.unknown_events?'仍有未知，無法宣稱完整總額':'所有目前帳本事件已有金額')}${metric('待核事件',l.unknown_events,'未知時核心會停止新派送')}${metric('主機 / 電力 / 人工','尚未計價','API 為零不代表完整營運成本為零')}</div>`+
 `${attribution(l)}<section class="panel mt"><div class="panel-header"><h2>預算與預留</h2>${badge(l.basis==='simulated_core_budget'?'模擬帳本':'核心帳本',l.basis==='simulated_core_budget'?'amber':'blue')}</div><div class="table-wrap"><table><thead><tr><th>帳戶</th><th>授權額度</th><th>已預留 / 暫扣</th><th>已知支出</th><th>可用</th><th>派送狀態</th></tr></thead><tbody>${l.accounts.map(a=>`<tr><td class="mono">${esc(a.id)}</td><td>${money(a.authorized_usd)}</td><td>${money(a.reserved_usd)}<br><span class="muted">待核暫扣 ${money(a.provisional_hold_usd)}</span></td><td>${money(a.known_usd)}</td><td>${money(a.available_usd)}</td><td>${badge(a.dispatch_blocked?'已阻擋':'以核心執行前檢查為準',a.dispatch_blocked?'red':'')}</td></tr>`).join('')}</tbody></table></div></section>`+
 `<section class="panel mt"><div class="panel-header"><h2>逐筆費用事件</h2><span class="muted">${l.rows.length} 筆 · 相同帳本只計一次</span></div><div class="table-wrap"><table><thead><tr><th>請求 / generation</th><th>種類 / 實際模型</th><th>金額 / 用量</th><th>帳務證據</th></tr></thead><tbody>${l.rows.slice(-200).reverse().map(r=>`<tr><td><code>${esc(r.id)}</code><br><span class="muted mono">${esc(r.receipt)}</span></td><td>${esc(r.request_kind==='classification'?'Jev 分類':r.request_kind==='generation'?'模型作答':r.request_kind)}<br><span class="muted">${esc(r.model || '模型缺證')}<br>${esc(r.provider || '供應商缺證')}</span></td><td class="amount">${money(r.cost)}<br><span class="muted">${r.tokens ? esc((r.tokens.prompt_tokens ?? r.tokens.input_tokens ?? '—')+' in / '+(r.tokens.completion_tokens ?? r.tokens.output_tokens ?? '—')+' out') : '用量缺證'}</span></td><td>${badge(r.cost===null?'費用未知':r.verified?'已核帳':'API 已報費用',r.cost===null?'amber':r.verified?'green':'blue')}</td></tr>`).join('')||'<tr><td colspan="4" class="empty">尚無帳務事件</td></tr>'}</tbody></table></div><div class="table-note">保存所有事件，依請求 ID 排序顯示末 200 筆。待核暫扣保留尚未最終核帳的費用上界。usage 金額與最終核帳不重複相加。已知分類 ${money(l.by_kind_known_usd.classification)}；已知作答 ${money(l.by_kind_known_usd.generation)}。</div></section>`;
}
function candidateObservation() {
 const observed=pool?.operator_observation?.schema_version==='mrc-observation-v1'?pool.operator_observation.candidates:null;
 return observed?.context?.category===selectedCategory&&observed.context.mode===selectedCandidateMode
   &&observed.context.pool_id===(selectedCandidateVersion||pool.id)?observed:null;
}
function candidate(c,i,tier) {
 const p=c.profile||{};
 return `<article class="pool-candidate"><div class="position">${['PRIMARY / 主選','BACKUP 01 / 備援','BACKUP 02 / 備援'][i]}</div>${p.model?`<div class="model-name">${esc(p.model)}</div><div class="provider">${esc(p.provider||'供應商未綁定')} · ${esc(p.effort||'強度未知')}</div>${badge(poolStatuses[c.status]||c.status,c.status==='pending_unqualified_stale_source'?'red':'amber')}<div class="property"><span>Quality / 原生分數</span><strong>${esc(c.quality)}</strong></div><div class="property"><span>Cost / 報價情境</span><strong>${money(c.quote_usd)}</strong></div><div class="property"><span>Speed / 端點 P50</span><strong>${c.endpoint_p50_ms===undefined||c.endpoint_p50_ms===null?'未知':esc(c.endpoint_p50_ms)+'ms'}</strong></div><div class="property"><span>完整任務成本</span><strong>${money(c.flow_cost_usd)}</strong></div><div class="property"><span>完整任務延遲</span><strong>${c.flow_latency_ms==null?'未知':esc(c.flow_latency_ms)+'ms'}</strong></div>`:'<div class="pool-short">尚無可對應的候選。<br>保留缺額，不以其他池補數。</div>'}${window.CloudyFanCandidates?.row(candidateObservation()?.tiers?.[tier]?.[i],tier,i,{pool_id:pool.id,category:selectedCategory,mode:selectedCandidateMode})||''}</article>`;
}
function completenessNotice(completeness) {
 if(!completeness)return '';
 const partial=completeness.status==='partial', failed=completeness.status==='failed';
 const title=failed?'更新失敗，保留舊版':partial?'部分更新':'來源取得完成';
 return `<div class="notice"><strong>最近來源更新嘗試</strong> ${badge(title,failed?'red':partial?'amber':'blue')}<br>已嘗試 ${esc(completeness.attempted)} 次 GET · 成功 ${esc(completeness.succeeded)} · 失敗 ${esc(completeness.failed)} · 模型端點未查 ${esc(completeness.deferred)}<br>缺少的資料維持未知，候選發布仍不授予執行資格。${completeness.failures?.length?`<details><summary>失敗來源</summary>${completeness.failures.map(failure=>`<p><span class="mono">${esc(failure.model||failure.url)}</span> · ${esc(errors[failure.code]||failure.code)}</p>`).join('')}</details>`:''}${completeness.deferred_models?.length?`<details><summary>未查模型端點</summary><p class="mono">${completeness.deferred_models.map(esc).join('、')}</p></details>`:''}</div>`;
}
function contractMatrix() {
 const capabilities=data.capabilities;
 if(!capabilities?.categories)return `<section class="panel mt"><div class="panel-header"><h2>類別 × 四模式契約</h2>${badge('尚無註冊資訊','amber')}</div><div class="panel-body">目前沒有可查核的逐類別契約資訊，不能由候選資料推定可派送。</div></section>`;
 return `<section class="panel mt"><div class="panel-header"><h2>類別 × 四模式契約</h2>${badge('已註冊仍須執行前檢查','blue')}</div><div class="table-note">目前路由版本 <span class="mono">${esc(capabilities.pool?.slice(0,12)||'尚未啟用')}</span>；這是執行契約註冊範圍，與正在查看的候選版本分開。預算、費用資格與其他執行條件仍由核心檢查。</div><div class="table-wrap"><table><thead><tr><th>分類</th>${Object.values(modes).map(mode=>`<th>${esc(mode)}</th>`).join('')}</tr></thead><tbody>${Object.entries(capabilities.categories).map(([category,registered])=>`<tr><td><span class="category-name">${esc(cname(category))}</span><span class="category-id">${esc(category)}</span></td>${Object.keys(modes).map(mode=>{const entry=registered[mode];return `<td>${badge(entry?.contract_registered?'契約已註冊':'未支援',entry?.contract_registered?'green':'amber')}<br><span class="muted">${entry?.contract_registered?esc(entry.contract_id):'缺少此類別與模式的契約'}</span></td>`;}).join('')}</tr>`).join('')}</tbody></table></div></section>`;
}
function poolsPage() {
 if(!pool)return heading('CANDIDATE POOLS','23 類候選池視圖','讀取中…');
 const category=pool.categories[selectedCategory]||Object.values(pool.categories)[0];
 const named=Object.values(pool.categories).flatMap(c=>Object.values(c.pools).flat()).filter(c=>c.profile?.model).length;
 const observed=candidateObservation(), versions=pool.operator_observation?.candidates?.versions||[{pool_id:pool.id}];
 const sameViewedVersion=pool.id===(selectedCandidateVersion||data.control.catalog_pool);
 return heading('CANDIDATE POOLS / QUALITY · COST · SPEED','23 類候選池視圖','高、中、低為品質分組；開源候選可與它們重疊。每欄主選與兩個備援，候選資料與執行資格分開。',`<button class="button primary" id="refresh-pools" ${busy||!data.source_refresh_available?'disabled':''}>${busy?'正在取得資料…':'手動更新來源'}</button>`)+
 completenessNotice(refreshCompleteness||pool.completeness)+
 `<div class="notice"><strong>${esc(({historical_local_inventory:'既有歷史盤點',offline_demo_refresh_no_external_get:'模擬更新 · 保留原資料時間',offline_mock_runtime:'模擬路由設定',openrouter_free_get_candidate_proposal:'OpenRouter 候選更新'})[pool.source_kind]||pool.source_kind)}</strong> · 資料時間 ${esc(when(pool.created_at))}<br>具名 ${named} / 276 個位置 · 缺額 ${276-named}。報價情境為 2,000 輸入 + 1,000 輸出 token，並非含升級的完整成本；歷史准入不代表目前可派送。<br><span class="mono">候選 ${esc(pool.id.slice(0,12))} · 路由 ${esc(data.control.routing_pool?.slice(0,12)||'尚未啟用')}</span></div>`+
 `<div class="filters"><label class="candidate-query-label" for="candidate-version">候選版本<select id="candidate-version">${versions.map(v=>`<option value="${esc(v.pool_id)}" ${v.pool_id===(selectedCandidateVersion||pool.id)?'selected':''}>${esc(v.pool_id.slice(0,12))}${v.active?' · 目前執行':''}${v.catalog?' · 目前候選':''}</option>`).join('')}</select></label><label class="candidate-query-label" for="category">任務分類<select id="category">${options(Object.fromEntries(Object.keys(pool.categories).map(c=>[c,cname(c)+' · '+c])),selectedCategory)}</select></label><label class="candidate-query-label" for="candidate-mode">觀測模式<select id="candidate-mode">${options(modes,selectedCandidateMode)}</select></label><span class="muted">品質單位：${esc(category.unit)}</span><button id="activate-pool" class="button small" ${pool.routing_available?'':'disabled'}>啟用此路由版本</button></div>`+
 (window.CloudyFanCandidates?.context(observed,candidateRead.selection===candidateSelectionKey()?candidateRead:{status:'loading'})||'')+
 (sameViewedVersion?`<div class="pool-grid">${Object.entries(candidateTiers).map(([tier,label])=>`<section class="pool-column"><div class="pool-title"><h3>${label}</h3><span class="muted mono">${category.pools[tier].filter(c=>c.profile?.model).length} / 3</span></div>${category.pools[tier].map((c,i)=>candidate(c,i,tier)).join('')}</section>`).join('')}</div>`:'<div class="notice">尚未取得所選版本的候選清單，原清單不代入此查詢。</div>')+
 (window.CloudyFanCandidates?.excluded(observed)||'')+
 contractMatrix()+
 (sameViewedVersion?`<section class="panel mt"><div class="panel-header"><h2>全部分類候選覆蓋</h2><span class="muted">點選類別查看主備模型</span></div><div class="table-wrap"><table class="pool-summary"><thead><tr><th>分類</th>${Object.values(candidateTiers).map(t=>`<th>${t}</th>`).join('')}</tr></thead><tbody>${Object.entries(pool.categories).map(([id,c])=>`<tr><td><button class="text-button" data-category="${esc(id)}"><span class="category-name">${esc(cname(id))}</span><span class="category-id">${esc(id)}</span></button></td>${Object.keys(tiers).map(tier=>`<td><span class="pill-count">${c.pools[tier].filter(p=>p.profile?.model).length}/3</span><br><span class="muted">${esc(c.pools[tier][0]?.profile?.model||'缺額')}</span></td>`).join('')}</tr>`).join('')}</tbody></table></div></section>`:'');
}
function clientsPage() {
 const client=data.clients.find(c=>c.id===selectedClient);
 return heading('CLIENTS / INDIVIDUAL CONTROL','每個客戶端，各自看得清。','依受信任的客戶端身分統計，不以 IP 或使用者自行填寫的名稱歸戶。')+
 `<div class="cards">${data.clients.map(c=>`<article class="client-card">${badge(c.recently_seen?'最近有活動':'閒置',c.recently_seen?'green':'')}<h2>${esc(c.label)}</h2><p class="mono">${esc(c.id)}</p><div class="client-numbers"><div><b>${c.stats.tasks}</b><small>任務</small></div><div><b>${pct(c.stats.final_rate)}</b><small>公開通過率</small></div><div><b>${c.stats.escalated}</b><small>進入補答</small></div></div>${modeControls(c.id,c.mode||'')}<p class="mt">實際新任務模式：${esc(modes[c.effective_mode])}<br>最後活動 ${esc(when(c.last_seen))}</p><button class="button small details" data-client="${esc(c.id)}">查看此客戶端儀表板 →</button></article>`).join('')}</div>`+
 (client?`<section class="panel mt"><div class="panel-header"><div><h2>${esc(client.label)} / 任務明細</h2><p>P50 ${secs(client.stats.latency_p50)} · P95 ${secs(client.stats.latency_p95)} · 未知 ${client.stats.unknown} 件</p></div></div>${taskRows(data.tasks.filter(t=>t.client===client.id))}</section>`:'')+
 `<section class="panel mt"><div class="panel-header"><h2>設定變更紀錄</h2>${badge('不含憑證、提示與答案')}</div><div class="table-wrap"><table><thead><tr><th>時間</th><th>事件</th><th>對象 / 設定</th></tr></thead><tbody>${data.audit.filter(a=>a.action==='configuration_changed').slice(0,12).map(a=>`<tr><td class="nowrap">${esc(when(a.at))}</td><td>設定已變更</td><td>${esc(a.data.client||'全域')} · ${esc(a.data.inherit?'跟隨全域':modes[a.data.mode]||'模型池版本')}</td></tr>`).join('')}</tbody></table></div></section>`;
}
function rememberObservationView() {
 document.querySelectorAll('[data-observation-detail]').forEach(detail=>{
   if(detail.open)expandedCandidateDetails.add(detail.dataset.observationDetail);
   else expandedCandidateDetails.delete(detail.dataset.observationDetail);
 });
 const active=document.activeElement;
 if(!$('#main').contains(active))return null;
 return {id:active.id,task:active.dataset.taskTraceToggle,
   detail:active.closest('details[data-observation-detail]')?.dataset.observationDetail,
   start:active.selectionStart,end:active.selectionEnd};
}
function restoreObservationView(focus,page) {
 document.querySelectorAll('[data-observation-detail]').forEach(detail=>{
   detail.open=expandedCandidateDetails.has(detail.dataset.observationDetail);
 });
 if(!focus)return;
 const target=focus.id?document.getElementById(focus.id):focus.task?
   Array.from(document.querySelectorAll('[data-task-trace-toggle]')).find(e=>e.dataset.taskTraceToggle===focus.task):
   Array.from(document.querySelectorAll('details[data-observation-detail]')).find(e=>e.dataset.observationDetail===focus.detail)?.querySelector('summary');
 const fallback=target||(page==='pools'?$('#candidate-version'):$('#task-search'))||$('#main');
 if(fallback===$('#main'))fallback.tabIndex=-1;
 fallback.focus({preventScroll:!!target});
 if(typeof focus.start==='number'&&target?.setSelectionRange)target.setSelectionRange(focus.start,focus.end);
}
function render() {
 if(!operatorSession)return;const page=location.hash.slice(1) in labels ? location.hash.slice(1) : 'overview';
 const focused=rememberObservationView();
 $('#crumb').textContent=labels[page];document.querySelectorAll('nav a').forEach(a=>{a.classList.toggle('active',a.dataset.page===page);if(a.dataset.page===page)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
 $('#environment').textContent=operatorSession.demo?'模擬上游 · 非真實成效':'受控核心 · 正式資格另列';
 $('#environment').className='badge '+(operatorSession.demo?'amber':'blue');
 if(page==='capacity'){window.CloudyFanCapacity.mount($('#main'),sessionContext());return;}
 if(page==='queue'){if(window.CloudyFanQueue)window.CloudyFanQueue.mount($('#main'),sessionContext());else $('#main').textContent='排隊觀測介面尚未載入，請重新讀取。';return;}
 if(page==='observations'){if(window.CloudyFanObservationPolicy)window.CloudyFanObservationPolicy.mount($('#main'),sessionContext());else $('#main').textContent='建立時觀測規則介面尚未載入，請重新讀取。';return;}
 if(!data){$('#main').innerHTML=heading('OPERATOR / VERIFIED',esc(labels[page]),'操作員身分已確認，正在讀取此身分的觀測資料。')+'<div class="panel empty" role="status">完整觀測資料尚未讀取；任務、費用與統計仍待確認。</div>';return;}
 if(page==='billing'){window.CloudyFanBilling.mount($('#main'),sessionContext());return;}
 $('#main').innerHTML=({overview,tasks:tasksPage,ledger:ledgerPage,pools:poolsPage,clients:clientsPage}[page])();
 bindTaskTraces();bindSimulation();
 restoreObservationView(focused,page);
 document.querySelectorAll('[data-change-mode]').forEach(button=>button.onclick=()=>{
   const client=button.dataset.changeMode,mode=document.querySelector(`[data-mode-client="${client}"]`).value || null, revision=data.control.revision;
   confirmAction('變更新任務的路由模式',`<p>${esc(client||'全域預設')} → <strong>${esc(modes[mode]||'跟隨全域')}</strong></p><p>既有、排隊中與中斷續跑任務保留原模式、模型池及嘗試上限。全大模型優先是可選功能，切換本身不會發送模型請求。</p>`,()=>api('/monitor/mode',{revision,mode,...(client?{client}:{})}));
 });
 if($('#task-search'))$('#task-search').oninput=e=>{search=e.target.value;const at=e.target.selectionStart;render();$('#task-search').focus();$('#task-search').setSelectionRange(at,at);};
 if($('#task-client'))$('#task-client').onchange=e=>{taskFilter=e.target.value;render();};
 if($('#task-mode'))$('#task-mode').onchange=e=>{modeFilter=e.target.value;render();};
 if($('#category'))$('#category').onchange=e=>{selectedCategory=e.target.value;loadCandidateView();};
 if($('#candidate-version'))$('#candidate-version').onchange=e=>{selectedCandidateVersion=e.target.value;loadCandidateView();};
 if($('#candidate-mode'))$('#candidate-mode').onchange=e=>{selectedCandidateMode=e.target.value;loadCandidateView();};
 document.querySelectorAll('[data-category]').forEach(b=>b.onclick=()=>{selectedCategory=b.dataset.category;loadCandidateView();window.scrollTo(0,0);});
 document.querySelectorAll('[data-client]').forEach(b=>b.onclick=()=>{selectedClient=b.dataset.client;render();});
 if($('#refresh-pools'))$('#refresh-pools').onclick=refreshPools;
 if($('#activate-pool'))$('#activate-pool').onclick=()=>{const revision=data.control.revision,id=pool.id;confirmAction('啟用新的路由版本','<p>只有已在伺服器註冊執行契約的版本可以啟用。新任務使用新版本，既有任務沿用舊版本。</p>',()=>api('/monitor/pools/activate',{revision,pool:id}));};
}
const candidateSelectionKey = () => JSON.stringify([selectedCandidateVersion,selectedCategory,selectedCandidateMode]);
const candidateQuery = id => '/monitor/pools?'+new URLSearchParams({id,category:selectedCategory,mode:selectedCandidateMode});
async function loadCandidateView() {
 candidateRead={status:'loading',selection:candidateSelectionKey()};
 render();
 await readCandidateView(selectedCandidateVersion||data.control.catalog_pool,true);
 render();
}
function readCandidateView(id,force=false) {
 const version=authenticationVersion,requestToken=token,selection=candidateSelectionKey();
 if(!force&&candidateRequest?.selection===selection&&candidateRequest.id===id)return candidateRequest.promise;
 const flight=++candidateFlight,request={selection,id,promise:null};
 const current=()=>sameLogin(version,requestToken)&&flight===candidateFlight&&selection===candidateSelectionKey()
   &&id===(selectedCandidateVersion||data?.control.catalog_pool);
 candidateRequest=request;
 request.promise=(async()=>{
   try{
     const nextPool=await api(candidateQuery(id));
     if(!current())return;
     pool=nextPool;candidateRead={status:'current',selection};
   }catch{
     if(!current())return;
     candidateRead={status:candidateObservation()?'stale':'unavailable',selection};
   }finally{
     if(candidateRequest===request)candidateRequest=null;
     if(current()&&location.hash==='#pools')render();
   }
 })();
 return request.promise;
}
async function refreshPools() {
 const version=authenticationVersion,value=token,current=()=>sameLogin(version,value)&&!!operatorSession;
 busy=true;render();
 try {
   const result=await api('/monitor/pools/refresh',{});if(!current())return;const revision=data.control.revision;
   refreshCompleteness=result.completeness||null;
   confirmAction('候選更新已準備，檢查差異',`${completenessNotice(refreshCompleteness)}<p>${result.demo?'本次為模擬更新，沒有外部 API。':`已嘗試 ${esc(result.request_count)} 次來源 GET，沒有模型作答請求。`} ${result.changes.length} 個候選欄有資料差異。</p><p>發布只更新候選清單；部分更新保留缺少資料為未知，實際路由仍使用已註冊版本，不會自動批准新模型。</p><div class="diff-list">${result.changes.map(c=>`<div class="diff-row"><strong>${esc(cname(c.category))} / ${esc(candidateTiers[c.tier])}</strong><br>${esc(c.before.join(' → '))}<br>更新為 ${esc(c.after.join(' → '))}</div>`).join('')||'主備模型無變更；來源時間或觀測資料已更新。'}</div>`,()=>api('/monitor/pools/publish',{revision,pool:result.pool}));
 } catch(e) {if(current()){refreshCompleteness=e.completeness||null;error('更新失敗，現有版本保留：'+e.message);}} finally {if(current()){busy=false;render();}}
}
function load(force=false) {
 if(!token||!operatorSession)return Promise.resolve({ok:false,stale:true});
 const version=authenticationVersion,requestToken=token;
 if(snapshotFlight){
   if(snapshotFlight.version===version&&snapshotFlight.token===requestToken){
     snapshotFlight.force=snapshotFlight.force||force;
     return snapshotFlight.promise;
   }
   // Background history waits for the old flight; authentication and direct reads do not.
   return snapshotFlight.promise.then(()=>sameLogin(version,requestToken)?load(force):{ok:false,stale:true});
 }
 const flight={version,token:requestToken,force,promise:null};
 snapshotFlight=flight;
 flight.promise=(async()=>{
   try{
     const next=await api('/monitor/snapshot');
     if(!sameLogin(version,requestToken))return {ok:false,stale:true};
     data=next;
     const viewedPool=selectedCandidateVersion||next.control.catalog_pool;
     if(viewedPool&&(flight.force||location.hash==='#pools'||!pool||pool.id!==viewedPool||!candidateObservation())){
       const pending=readCandidateView(viewedPool,flight.force);
       if(!pool)await pending;
     }
     if(!sameLogin(version,requestToken))return {ok:false,stale:true};
     $('#updated').textContent='更新 '+new Date().toLocaleTimeString('zh-TW',{hour12:false});$('#error').hidden=true;
     if(flight.force||(!['INPUT','SELECT'].includes(document.activeElement?.tagName)&&!$('dialog[open]')))render();
     return {ok:true};
   }catch(e){
     if(!sameLogin(version,requestToken))return {ok:false,stale:true};
     error('資料未更新，保留上次畫面：'+e.message);
     return {ok:false,error:e};
   }finally{if(snapshotFlight===flight)snapshotFlight=null;}
 })();
 return flight.promise;
}
$('#reload').onclick=async()=>{
 if(!operatorSession)return;
 const version=authenticationVersion,value=token,page=location.hash;
 const history=load(true);
 if(page==='#capacity')await window.CloudyFanCapacity.refresh(true);
 else if(page==='#queue')await window.CloudyFanQueue?.refresh(true);
 else if(page==='#observations')await window.CloudyFanObservationPolicy?.refresh(true);
 else{await history;if(page==='#billing'&&sameLogin(version,value)&&operatorSession)await window.CloudyFanBilling.refresh();}
};
$('#logout').onclick=()=>{setToken('');if(!$('#login').open)$('#login').showModal();};
$('#login-form').onsubmit=async e=>{
 e.preventDefault();const result=await authenticate($('#token').value);
 if(result.stale)return;
 if(result.ok){$('#login').close();$('#login-error').textContent='';void load(true);}
 else $('#login-error').textContent=result.error?.message||'登入身分無法確認，請重新認證。';
};
window.addEventListener('hashchange',render);
setInterval(()=>load(),5000);
(async()=>{
 const version=authenticationVersion;
 try{
   const r=await fetch('/demo/session');
   if(authenticationVersion!==version)return;
   if(r.ok){const session=await r.json();if(authenticationVersion!==version)return;const result=await authenticate(session.token);if(result.ok)void load(true);else if(!result.stale){if(!$('#login').open)$('#login').showModal();$('#login-error').textContent=result.error?.message||'登入身分無法確認。';}}
   else $('#login').showModal();
 }catch{if(authenticationVersion===version)error('無法連線至 monitor。');}
})();
