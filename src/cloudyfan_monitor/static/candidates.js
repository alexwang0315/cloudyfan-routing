'use strict';
// Operator-only, allowlisted candidate evidence. It never initiates a dispatch.
window.CloudyFanCandidates = (() => {
  const esc = value => String(value ?? '未知').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const badge = (label, color='') => `<span class="badge ${color}">${esc(label)}</span>`;
  const statuses = {evidence_recorded:['三維證據已記錄','blue'],not_provided:['資格證據未提供','amber'],
    unverifiable:['證據無法確認','red'],expired:['資格證據已過期','red'],not_applicable:['不適用',''],
    unknown:['未知','amber'],not_checked:['尚未檢查','']};
  const scopes = {synthetic:'合成示範',research:'研究准入',unverified:'來源未核定'};
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
    candidate_evidence_unavailable:'原始原因尚無安全對照說明；證據維持無法確認。'};
  const status = value => badge(...(statuses[value] || statuses.unknown));
  const time = value => value ? esc(new Date(value).toLocaleString('zh-TW',{hour12:false})) : '未知';
  const pair = (key,value) => `<dt>${esc(key)}</dt><dd>${value}</dd>`;
  function reasonList(codes) {
    return `<ul class="candidate-reasons">${(codes || []).map(code=>`<li>${esc(reasons[code] || reasons.candidate_evidence_unavailable)} <code>${esc(code)}</code></li>`).join('')}</ul>`;
  }
  function context(observed) {
    if(!observed)return '<div id="candidate-context" class="notice">候選觀測未提供。保留既有候選清單，不推定資格或授權。</div>';
    const c=observed.context;
    return `<div id="candidate-context" class="notice" aria-live="polite"><div class="candidate-flags">${badge(c.active?'目前啟用版本':'未啟用版本',c.active?'blue':'amber')}${badge(c.contract_registered?'契約已登錄':'契約未登錄',c.contract_registered?'green':'amber')}${badge('派送授權尚未檢查')}</div><p>候選版本 <code>${esc(c.pool_id)}</code><br>類別 <code>${esc(c.category)}</code> · 模式 <code>${esc(c.mode)}</code><br>適用契約 <code>${esc(c.contract_id)}</code> · 契約版本 <code>${esc(c.contract_version)}</code><br>判定時點 ${time(observed.observed_at)}</p><p>列入候選、契約登錄、三維證據、健康與配額分開查核；實際派送仍由原任務及預算閘門決定。</p></div>`;
  }
  function row(value,tier,index) {
    if(!value)return '<div class="candidate-evidence muted">候選觀測尚未提供；原清單資料不等於執行資格。</div>';
    const q=value.qualification,e=q.evidence,c=value.configuration,missing=value.identity.missing_fields || [];
    const cost=e?.complete_cost_usd;
    return `<section class="candidate-evidence" data-candidate-evidence="${esc(tier+':'+index)}"><div class="candidate-flags">${badge(scopes[value.scope] || '來源未核定','amber')}${status(q.status)}${badge(q.formal_status==='not_applicable'?'正式資格不適用':'正式資格未知')}${badge('配額未知')}</div>${missing.length?`<p class="candidate-missing">設定身分缺項：${missing.map(key=>esc(fields[key] || key)).join('、')}</p>`:''}<details><summary>查看候選證據與理由</summary><dl class="candidate-properties">${Object.entries(fields).map(([key,label])=>pair(label,`<code>${esc(c[key])}</code>`)).join('')}${pair('清單位置',esc(value.listing_status==='missing'?'缺額':value.role==='primary'?'主選':'備援'))}${pair('契約設定對照',value.registration.candidate_matches_contract===true?'精確對應':value.registration.candidate_matches_contract===false?'未對應':'尚未檢查')}${pair('健康','未提供')}${pair('供應商配額','未知')}${pair('任務派送授權','尚未檢查')}${e?pair('工作量範圍',`<code>${esc(e.workload_id)}</code>`)+pair('證據類別',`<code>${esc(e.category_id)}</code>`)+pair('證據來源指紋',`<code>${esc(e.raw_sha256)}</code>`)+pair('證據取得時間',time(e.observed_at))+pair('證據有效至',time(e.valid_until))+pair('品質證據',e.quality_sufficient?'該範圍品質條件已記錄；非通用答對率':'未知')+pair('完整成本估計',cost?`USD ${esc(cost.lower)} ～ ${esc(cost.upper)}；非實付或硬上界`:'未知')+pair('流程速度估計',e.speed?`${esc(e.speed.metric)} / ${esc(e.speed.statistic)} 上緣 ${esc(e.speed.upper)} ms；非每次完成承諾`:'未知'):''}</dl>${reasonList(value.reason_codes)}</details></section>`;
  }
  function excluded(observed) {
    if(!observed?.excluded?.length)return '';
    return `<section class="panel mt"><div class="panel-header"><h2>準備時已排除的候選</h2>${badge('僅呈現原有識別與原因')}</div><div class="panel-body">${observed.excluded.map(row=>`<details class="candidate-excluded"><summary>設定 <code>${esc(row.config_id)}</code> · ${esc(row.tier)}</summary>${reasonList(row.reason_codes)}<p class="muted">未保存的完整設定維持未知，不補入主備位置。</p></details>`).join('')}</div></section>`;
  }
  return {context,row,excluded};
})();
