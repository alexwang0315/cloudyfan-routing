'use strict';
// Operator-only, allowlisted candidate evidence. It never initiates a dispatch.
window.CloudyFanCandidates = (() => {
  const esc = value => String(value ?? '未知').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const badge = (label, color='') => `<span class="badge ${color}">${esc(label)}</span>`;
  const statuses = {evidence_recorded:['三維證據已記錄','blue'],not_provided:['資格證據未提供','amber'],
    unverifiable:['證據無法確認','red'],expired:['資格證據已過期','red'],not_applicable:['不適用',''],
    unknown:['未知','amber'],not_checked:['尚未檢查','']};
  const scopes = {synthetic:'合成示範',research:'研究准入',unverified:'來源未核定'};
  const healthStatuses = {healthy:['健康條件通過','green'],missing:['健康資料缺少','amber'],
    stale:['健康已過期','red'],degraded:['健康條件未通過','red'],cooldown:['冷卻中','red'],
    recovery_required:['待明確恢復','amber'],not_provided:['健康未提供','amber'],
    not_applicable:['健康不適用',''],unverifiable:['健康證據無法確認','red'],
    unavailable:['健康資料無法讀取','red'],unknown:['健康未知','amber']};
  const freshness = {fresh:'原觀測仍在時效內',stale:'原觀測已過期',future:'來源時點不可採信',missing:'沒有精確觀測',unknown:'時效未知'};
  const fields = {config_id:'設定識別',configuration_sha256:'設定指紋',model:'模型',model_version:'模型版本',
    effort:'推理強度',provider:'供應商',quantization:'量化',backend:'執行後端'};
  const reasons = {candidate_qualification_not_provided:'沒有可對應到此設定的三維候選資格證據。',
    formal_qualification_not_evaluated:'這份觀測沒有核定正式資格，也不代表任務已取得派送授權。',
    candidate_identity_incomplete:'設定身分缺項，無法完成精確對照。',candidate_identity_mismatch:'候選設定與此契約的設定無法精確對應。',
    qualification_category_mismatch:'資格證據屬於另一個任務類別，不能挪用。',
    quality_evidence_insufficient:'尚無證據支持此設定符合本工作量的品質條件。',
    qualification_evidence_from_future:'證據取得時間晚於目前判定時點，不能當成目前適用的證據。',
    contract_not_registered:'此版本、類別與模式尚未登錄執行契約。',candidate_position_missing:'此位置仍是缺額，不以其他池補位。',
    candidate_evidence_recorded:'已從受控核心取得此設定的三維證據引用；適用範圍及效期如下。',
    qualification_evidence_expired:'原有資格證據已過有效期，未重新核定。',
    candidate_observation_not_supported:'此執行器尚未提供候選觀測。',duplicate_model_identity:'同一模型身分已列入，未重複當成另一個備援。',
    qualified_beyond_target:'準備紀錄中未選入主備位置，並不代表模型答錯。',
    missing_or_invalid_dimensions:'缺少適用的完整三維資料。',
    capability_context_or_provider_mismatch:'能力、上下文或供應商條件不符。',
    candidate_evidence_unavailable:'原始原因尚無安全對照說明；證據維持無法確認。',
    health_observation_missing:'沒有此精確設定與供應商的健康觀測，不能借用同名模型資料。',
    observation_in_future:'觀測晚於判定或發布時點，不能靠等待把原來源變成可信證據。',
    observation_stale:'原健康觀測已達既定時效，讀取不延長有效時間。',
    status_unknown:'來源沒有提供可確認的端點狀態。',status_degraded:'來源端點狀態不符合健康政策。',
    snapshot_in_future:'健康快照發布時間晚於目前判定時點，證據無法確認。',
    cooldown_active:'既有失敗事件已開啟熔斷，原冷卻期限尚未結束。',
    recovery_required:'冷卻已結束，但仍須新且合格的觀測及明確恢復；讀取不會關閉熔斷。',
    health_identity_unverifiable:'健康資料與此候選的精確設定或適用類別無法可信對應。',
    health_policy_unverifiable:'凍結契約與目前健康來源的政策無法精確對應。',
    health_evidence_unavailable:'這列健康資料目前無法讀取或驗證，其他列仍分別查核。',
    health_reason_unavailable:'此健康原因尚無安全對照，維持無法確認，不顯示私人錯誤。'};
  const status = value => badge(...(statuses[value] || statuses.unknown));
  const time = value => value ? esc(new Date(value).toLocaleString('zh-TW',{hour12:false})) : '未知';
  const pair = (key,value) => `<dt>${esc(key)}</dt><dd>${value}</dd>`;
  function healthReason(code) {
    const uptime=typeof code==='string'&&code.match(/^(uptime_last_(5m|30m|1d))_(unit_unknown_or_invalid|unknown|below_minimum)$/);
    if(uptime)return `${({'5m':'5 分鐘','30m':'30 分鐘','1d':'1 天'})[uptime[2]]}原生可用率${({unit_unknown_or_invalid:'單位缺少或不適用',unknown:'資料缺少',below_minimum:'低於既定最低條件'})[uptime[3]]}。`;
    const speed=typeof code==='string'&&code.match(/^(latency|throughput)_(unit_unknown_or_invalid|invalid|outside_limit|p(?:50|75|90|99)_unknown)$/);
    if(!speed)return null;
    const field=speed[1]==='latency'?'端點延遲':'端點吞吐量', detail=speed[2];
    return `${field}${({unit_unknown_or_invalid:'單位缺少或不適用',invalid:'數值無法確認',outside_limit:'不符合政策限制'})[detail]||`${detail.split('_')[0]} 分位資料缺少`}；這是來源端點觀測。`;
  }
  function reasonList(codes, extra='', fallback='candidate_evidence_unavailable') {
    if(!codes?.length)return '';
    return `<ul class="candidate-reasons ${extra}">${(codes || []).map(code=>{
      const explanation=Object.hasOwn(reasons,code)?reasons[code]:healthReason(code),safe=explanation?code:fallback;
      return `<li>${esc(explanation||reasons[fallback])} <code>${esc(safe)}</code></li>`;
    }).join('')}</ul>`;
  }
  function healthDetails(h) {
    const m=h.endpoint_metrics||{},c=h.circuit;
    const percentile = field => Object.entries(m[field]||{}).map(([key,value])=>`${esc(key)}: ${esc(value)}`).join(' · ')||'未知';
    return `<div class="candidate-health-details"><h4>目前健康證據</h4><p class="muted">健康只反映此設定、供應商與政策的目前可用條件；不核定能力、配額或任務派送授權。</p><dl class="candidate-properties candidate-health-properties">${pair('健康判定時間',time(h.assessed_at))}${pair('原觀測時間',time(h.observed_at))}${pair('資料時效',esc(freshness[h.freshness]||freshness.unknown))}${pair('原始時效上限',h.ttl_seconds==null?'未知':`${esc(h.ttl_seconds)} 秒；讀取不續期`)}${pair('健康快照',`<code>${esc(h.snapshot_id)}</code>`)}${pair('適用政策指紋',`<code>${esc(h.policy_sha256)}</code>`)}${pair('健康政策條件',h.policy_eligible===true?'通過':h.policy_eligible===false?'未通過':'無法確認')}${pair('熔斷',c?c.open?'開啟':'未開啟':'未知')}${pair('既有失敗事件數',esc(c?.failures))}${pair('最後失敗時間',time(c?.last_failure_at))}${pair('原冷卻期限',time(c?.cooldown_until))}${pair('原生可用率',`5 分鐘 ${esc(m.uptime_last_5m)} · 30 分鐘 ${esc(m.uptime_last_30m)} · 1 天 ${esc(m.uptime_last_1d)} ${m.uptime_unit==='percent'?'%':'（單位未知）'}`)}${pair('端點延遲',`${percentile('latency_last_30m')} ${m.latency_unit==='milliseconds'?'ms':'（單位未知）'}`)}${pair('端點吞吐量',`${percentile('throughput_last_30m')} ${m.throughput_unit==='tokens/second'?'token/s':'（單位未知）'}`)}</dl><p class="muted">端點觀測，非整件任務速度；未提供的分位數維持未知。</p>${reasonList(h.reason_codes,'candidate-health-reasons','health_reason_unavailable')}</div>`;
  }
  function context(observed,reading={}) {
    if(!observed)return `<div id="candidate-context" class="notice" aria-live="polite">${reading.status==='unavailable'?'此查詢目前無法取得候選觀測。':'此查詢的候選觀測尚未取得。'}保留既有候選清單，不推定資格或授權。</div>`;
    const c=observed.context||{},stale=reading.status==='stale',unavailable=observed.status==='unavailable';
    const notice=stale?'<p><strong>本次讀取失敗，以下是上次候選觀測的舊資料。</strong></p>':unavailable?
      '<p><strong>目前候選觀測不可用；沒有可確認的資格或健康判定。</strong></p>':observed.status==='partial'?
      '<p><strong>部分候選證據無法確認，其餘可驗證內容仍列於下方。</strong></p>':'';
    return `<div id="candidate-context" class="notice" aria-live="polite">${notice}<div class="candidate-flags">${badge(c.active?'目前啟用版本':'未啟用版本',c.active?'blue':'amber')}${badge(c.contract_registered===true?'契約已登錄':c.contract_registered===false?'契約未登錄':'契約狀態未知',c.contract_registered===true?'green':'amber')}${badge('派送授權尚未檢查')}</div><p>候選版本 <code>${esc(c.pool_id)}</code><br>類別 <code>${esc(c.category)}</code> · 模式 <code>${esc(c.mode)}</code><br>適用契約 <code>${esc(c.contract_id)}</code> · 契約版本 <code>${esc(c.contract_version)}</code><br>${stale?'原判定時點':'判定時點'} ${time(observed.observed_at)}</p><p>列入候選、契約登錄、三維證據、健康與配額分開查核；實際派送仍由原任務及預算閘門決定。</p></div>`;
  }
  function row(value,tier,index,context) {
    if(!value)return '<div class="candidate-evidence muted">候選觀測尚未提供；原清單資料不等於執行資格。</div>';
    const q=value.qualification,e=q.evidence,c=value.configuration,h=value.health||{status:'unknown'},missing=value.identity.missing_fields || [];
    const cost=e?.complete_cost_usd;
    const key=JSON.stringify([context?.pool_id,context?.category,context?.mode,tier,index,c.configuration_sha256||c.config_id]);
    return `<section class="candidate-evidence" data-candidate-evidence="${esc(tier+':'+index)}"><div class="candidate-flags">${badge(scopes[value.scope] || '來源未核定','amber')}${status(q.status)}${badge(q.formal_status==='not_applicable'?'正式資格不適用':'正式資格未知')}${badge('配額未知')}<span data-candidate-health-status="${esc(h.status)}">${badge(...(healthStatuses[h.status]||healthStatuses.unknown))}</span></div>${missing.length?`<p class="candidate-missing">設定身分缺項：${missing.map(key=>esc(fields[key] || key)).join('、')}</p>`:''}<details data-observation-detail="${esc(key)}"><summary>查看候選證據與理由</summary>${healthDetails(h)}<dl class="candidate-properties">${Object.entries(fields).map(([key,label])=>pair(label,`<code>${esc(c[key])}</code>`)).join('')}${pair('清單位置',esc(value.listing_status==='missing'?'缺額':value.role==='primary'?'主選':'備援'))}${pair('契約設定對照',value.registration.candidate_matches_contract===true?'精確對應':value.registration.candidate_matches_contract===false?'未對應':'尚未檢查')}${pair('供應商配額','未知')}${pair('任務派送授權','尚未檢查')}${e?pair('工作量範圍',`<code>${esc(e.workload_id)}</code>`)+pair('證據類別',`<code>${esc(e.category_id)}</code>`)+pair('證據來源指紋',`<code>${esc(e.raw_sha256)}</code>`)+pair('證據取得時間',time(e.observed_at))+pair('證據有效至',time(e.valid_until))+pair('品質證據',e.quality_sufficient?'該範圍品質條件已記錄；非通用答對率':'未知')+pair('完整成本估計',cost?`USD ${esc(cost.lower)} ～ ${esc(cost.upper)}；非實付或硬上界`:'未知')+pair('流程速度估計',e.speed?`${esc(e.speed.metric)} / ${esc(e.speed.statistic)} 上緣 ${esc(e.speed.upper)} ms；非每次完成承諾`:'未知'):''}</dl>${reasonList(value.reason_codes)}</details></section>`;
  }
  function excluded(observed) {
    if(!observed?.excluded?.length)return '';
    return `<section class="panel mt"><div class="panel-header"><h2>準備時已排除的候選</h2>${badge('僅呈現原有識別與原因')}</div><div class="panel-body">${observed.excluded.map((row,index)=>`<details class="candidate-excluded" data-observation-detail="${esc(JSON.stringify([observed.context?.pool_id,observed.context?.category,observed.context?.mode,'excluded',row.tier,row.config_id,index]))}"><summary>設定 <code>${esc(row.config_id)}</code> · ${esc(row.tier)}</summary>${reasonList(row.reason_codes)}<p class="muted">未保存的完整設定維持未知，不補入主備位置。</p></details>`).join('')}</div></section>`;
  }
  return {context,row,excluded};
})();
