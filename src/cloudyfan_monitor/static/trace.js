/* Read-only task evidence. No prompts, responses, credentials or fetches. */
'use strict';
window.CloudyFanTrace = (() => {
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const knownText = value => typeof value === 'string' && value ? escape(value) : '未知';
  const tiers = {open_source:'開源池',low:'低分池',mid:'中分池',high:'高分池'};
  const knownTier = value => typeof value === 'string' && Object.hasOwn(tiers,value);
  const tierName = value => knownTier(value) ? tiers[value] : '未知模型池';
  const cost = value => typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value) ? '$' + escape(value) : '未知';
  const quantity = value => (typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value)) || (typeof value === 'number' && Number.isFinite(value) && value >= 0) ? escape(value) : '未知';
  const tokens = value => (typeof value === 'string' && /^\d+$/.test(value)) || (Number.isSafeInteger(value) && value >= 0) ? escape(value) : '未知';
  const badge = (label,color = '') => `<span class="badge ${color}">${escape(label)}</span>`;
  const verdict = passed => passed === true ? ['公開驗收通過','green'] : passed === false ? ['公開驗收未通過','red'] : ['公開驗收未知','amber'];
  const record = value => value && typeof value === 'object' && !Array.isArray(value);
  const stopLabels = {
    passed:'公開驗收通過',public_validation_passed:'公開驗收通過',failed:'公開驗收未通過',
    public_validation_failed:'公開驗收未通過',public_validation_unavailable:'公開驗收無法確認',
    unverifiable:'公開驗收無法確認',max_attempts:'已達嘗試上限',attempt_limit:'已達嘗試上限',
    completed:'此次執行已結束',budget_exceeded:'預算不足，停止後續派送',
    policy_denied:'執行條件不符',engineering_interrupted:'工程中斷',
    accounting_interrupted:'費用待核，停止後續派送',unknown_cost:'費用未知，停止後續派送',
    service_busy:'執行容量已滿',task_expired:'任務存取已到期',
    stop:'模型已結束此次回應',final:'模型已返回最終回應',
    empty_or_exhausted:'回應內容為空，未取得最終答案',turn_limit:'已達本次作答回合上限',
    provider_unavailable:'供應商暫時無法提供服務',provider_rate_limited:'供應商請求受限，停止本次嘗試'
  };
  const fallbackLabels = {
    invalid_classifier_output:'分類回傳格式無法使用',invalid_output:'分類回傳格式無法使用',
    invalid_choice:'分類未回傳可用選項',invalid_tier:'分類未回傳可用模型池',
    classifier_unavailable:'分類服務無法取得結果',classification_unavailable:'分類服務無法取得結果',
    classifier_failed:'分類呼叫未完成',classification_failed:'分類呼叫未完成',
    timeout:'分類呼叫逾時',invalid_probabilities:'分類分布無法核對',
    no_eligible_candidate:'原選項沒有符合條件的模型',missing_probability:'缺少分類分布',
    malformed_response:'分類回傳格式無法使用',unparseable_response:'分類回傳格式無法使用',
    parse_failure:'分類回應格式或內容無法核對，改採保守模型池',
    tied_or_inconsistent_distribution:'分類分布相同或與回報選項矛盾，改採保守模型池',
    distribution_missing:'未提供分類分布，改採保守模型池',
    explicit_abstention_choice:'分類明確選擇不作判斷，改採保守模型池'
  };
  const labelFrom = (labels,value) => typeof value === 'string' && Object.hasOwn(labels,value) ? labels[value] : '未知';

  function time(value) {
    if((typeof value !== 'string' || !value) && typeof value !== 'number') return '未知';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isFinite(date.getTime()) ? escape(date.toLocaleString('zh-TW',{hour12:false})) : '未知';
  }
  function poolCards(task,trace,attempts,decisions) {
    const path = Array.isArray(task.path) ? task.path.filter(record) : [];
    const complete = Array.isArray(trace.attempts) && Array.isArray(trace.decisions)
      && trace.attempts.length === attempts.length && trace.decisions.length === decisions.length
      && attempts.every(attempt => knownTier(attempt.tier))
      && decisions.every(decision => knownTier(decision.selected_tier))
      && path.every(item => attempts.some(attempt => attempt.tier === item.tier));
    const terminal = ['passed','failed','unverifiable','engineering_interrupted','accounting_interrupted','policy_denied','expired'].includes(task.status);
    return `<div class="trace-pool-grid">${Object.entries(tiers).map(([tier,label]) => {
      const matching = attempts.filter(attempt => attempt.tier === tier);
      const selected = decisions.some(decision => decision.selected_tier === tier);
      let state = ['未知','amber'], note = '缺少完整選池紀錄';
      if(matching.length) { state = verdict(matching.at(-1).passed); note = `已嘗試 ${matching.length} 次，顯示最近一次結果`; }
      else if(selected) { state = ['已選，作答資料待確認','blue']; note = '已記錄 Jev 選池，尚無此池的作答紀錄'; }
      else if(complete && attempts.length && terminal) { state = ['未選','']; note = '已記錄的完整流程沒有使用此池'; }
      else if(complete && !terminal) { state = ['尚未嘗試','blue']; note = '任務仍可能繼續，後續選池尚未確認'; }
      const models = [...new Set(matching.flatMap(attempt => [
        ...(Array.isArray(attempt.models) ? attempt.models : []),
        ...(Array.isArray(attempt.calls) ? attempt.calls.filter(record).map(call => call.model) : [])
      ]).filter(model => typeof model === 'string' && model))];
      return `<section class="trace-pool" data-trace-tier="${tier}"><h4>${label}</h4>${badge(...state)}<p>${note}</p>${models.length ? `<p class="trace-model-list mono">${models.map(escape).join('<br>')}</p>` : ''}</section>`;
    }).join('')}</div>`;
  }
  function callRows(calls) {
    if(!Array.isArray(calls)) return '<p class="trace-empty">呼叫明細未知。</p>';
    const rows = calls.filter(record);
    if(!rows.length) return '<p class="trace-empty">尚無已記錄的模型呼叫。</p>';
    return `<div class="table-wrap"><table class="trace-call-table"><thead><tr><th>實際模型／供應商</th><th>輸入 tokens</th><th>輸出 tokens</th><th>API 費用 · USD</th><th>呼叫時間 · ms</th><th>供應商請求編號</th></tr></thead><tbody>${rows.map(call => `<tr><td><span class="mono">${knownText(call.model)}</span><br><span class="metadata muted">${knownText(call.provider)}</span></td><td class="mono">${tokens(call.input_tokens)}</td><td class="mono">${tokens(call.output_tokens)}</td><td class="mono trace-cost">${cost(call.cost_usd)}</td><td class="mono">${quantity(call.elapsed_ms)}</td><td class="trace-generation"><code>${knownText(call.generation_id)}</code></td></tr>`).join('')}</tbody></table></div>`;
  }
  function attemptsSection(trace,attempts) {
    return `<section class="trace-section"><div class="trace-section-title"><h3>模型作答時間線</h3><span class="metadata muted">依已記錄的嘗試順序</span></div>${!Array.isArray(trace.attempts) ? '<p class="trace-empty">作答時間線未知，無法推定呼叫次數或未選模型池。</p>' : !attempts.length ? '<p class="trace-empty">目前沒有已記錄的作答嘗試。</p>' : `<ol class="trace-timeline">${attempts.map((attempt,index) => {
      const order = Number.isSafeInteger(attempt.index) && attempt.index > 0 ? `第 ${attempt.index} 次嘗試` : `嘗試紀錄 ${index + 1}`;
      const models = Array.isArray(attempt.models) ? attempt.models.filter(model => typeof model === 'string' && model) : [];
      return `<li class="trace-attempt" data-trace-attempt><div class="trace-attempt-heading"><div><span class="trace-order">${order}</span><h4>${tierName(attempt.tier)}</h4></div>${badge(...verdict(attempt.passed))}</div><p class="trace-stop">停止或接續原因：${labelFrom(stopLabels,attempt.stop)}</p>${models.length ? `<p class="trace-attempt-models">此嘗試模型：<span class="mono">${models.map(escape).join('、')}</span></p>` : ''}${callRows(attempt.calls)}</li>`;
    }).join('')}</ol>`}</section>`;
  }
  function probabilities(value) {
    if(!record(value)) return '<span class="muted">分布未知</span>';
    const entries = Object.entries(tiers).filter(([tier]) => Object.hasOwn(value,tier));
    if(!entries.length) return '<span class="muted">分布未知</span>';
    return `<div class="trace-probabilities">${entries.map(([tier,label]) => `<span>${label}<strong class="mono">${typeof value[tier] === 'string' && /^(?:0(?:\.\d+)?|1(?:\.0+)?)$/.test(value[tier]) ? escape(value[tier]) : '未知'}</strong></span>`).join('')}</div>`;
  }
  function decisionsSection(trace,decisions) {
    // Backup policy/economic choices share the historical decision array, but
    // only a saved classifier identity establishes an actual Jev call here.
    decisions = decisions.filter(decision => typeof decision.generation_id === 'string'
      && decision.generation_id && typeof decision.model === 'string' && decision.model);
    const stages = {low_mid:'第一階段 · 低／中分池選擇',mid_high:'第二階段 · 中／高分池選擇'};
    const triggers = {public_validation_failed:'前次公開驗收未通過',public_validation_unavailable:'前次公開驗收無法確認'};
    return `<section class="trace-section"><div class="trace-section-title"><h3>Jev 階段決策</h3><span class="badge amber">分布不是答對機率</span></div>${!Array.isArray(trace.decisions) ? '<p class="trace-empty">Jev 階段紀錄未知，無法推定未呼叫分類。</p>' : !decisions.length ? '<p class="trace-empty">目前沒有已記錄的 Jev 階段決策。</p>' : `<div class="trace-decisions">${decisions.map(decision => `<article class="trace-decision" data-trace-decision><div class="trace-attempt-heading"><h4>${labelFrom(stages,decision.stage)}</h4>${badge(decision.fallback === true ? '已採保守備援選池' : decision.fallback === false ? '未採保守備援選池' : '保守備援狀態未知',decision.fallback === true ? 'amber' : '')}</div><dl class="trace-facts"><div><dt>觸發原因</dt><dd>${labelFrom(triggers,decision.trigger)}</dd></div><div><dt>前次模型池</dt><dd>${tierName(decision.previous_tier)}</dd></div><div><dt>Jev 回報選項</dt><dd>${tierName(decision.reported_choice)}</dd></div><div><dt>實際選池</dt><dd><strong>${tierName(decision.selected_tier)}</strong></dd></div><div><dt>保守備援原因</dt><dd>${decision.fallback === false ? '未啟用' : labelFrom(fallbackLabels,decision.fallback_reason)}</dd></div><div><dt>分類模型</dt><dd class="mono">${knownText(decision.model)}</dd></div><div><dt>分類 API 費用 · USD</dt><dd class="mono">${cost(decision.cost_usd)}</dd></div><div><dt>供應商請求編號</dt><dd><code>${knownText(decision.generation_id)}</code></dd></div></dl><div class="trace-distribution"><h5>Jev 回報分布 · 0–1</h5>${probabilities(decision.probabilities)}<p>此分布用來比較模型池選項，不表示答案通過驗收的機率。</p></div></article>`).join('')}</div>`}</section>`;
  }
  const historyReasons = {
    historical_evidence_unavailable:'當時證據無法確認，保留既有呼叫與費用紀錄',
    historical_evidence_not_complete:'任務尚未完成或原證據目前無法確認，不補造當時理由',
    candidate_history_not_provided:'未提供逐候選歷史證據；沒有紀錄不能推定未呼叫',
    runner_observation_not_provided:'此 runner 未提供歷史觀測；既有明細仍可查看',
    saved_historical_selection:'依原任務凍結候選與已保存選擇紀錄呈現',
    saved_qcs_selection:'依原任務保存的三維選模決策呈現，未保存的健康與估計仍為未知',
    classified_flow_estimate_unknown:'已保存成本比較，但缺少完整分類流程耗時估計，採直接路徑',
    classification_estimate_unknown:'缺少分類費或分布估計，採直接路徑',
    classification_exceeds_speed_limit:'完整分類流程耗時超出原限制，採直接路徑',
    single_eligible_candidate:'當時只有一個符合資格的選項，採直接路徑',
    saved_selection_unverifiable:'部分選擇證據無法確認，其餘已確認明細保留',
    exact_classifier_calibration_missing:'缺少精確分類校準，不能確認支付分類費是否有利，採直接路徑',
    classification_not_economic:'已有校準，但保守淨節省不足或耗時未達限制，採直接路徑',
    conservative_net_saving_positive:'保守淨節省足夠，已呼叫 Jev',
    same_pool_availability_or_initial:'依原任務模式與當時候選選模（首答或備援）',
    no_eligible_candidate:'當時沒有符合派送條件的候選',
    public_validation_passed:'先前公開驗收已通過，未進入此階段',
    provider_failed_this_run:'此供應商在原任務已發生可用性故障',
    configuration_already_dispatched:'此設定已在原任務派送',
    observation_missing:'當時缺少健康觀測',health_observation_missing:'當時缺少健康觀測',observation_stale:'當時健康觀測已過期',
    snapshot_in_future:'健康快照發布時間晚於當時判定時間',
    observation_in_future:'健康觀測時間晚於當時判定時間',status_unknown:'當時端點狀態未知',
    status_degraded:'當時端點狀態降級',circuit_open:'當時熔斷尚未解除',
    cooldown_active:'當時仍在冷卻',recovery_required:'當時仍需恢復證據',
    candidate_qualification_stale_or_invalid:'當時候選資格過期或無法確認',
    local_backend_not_registered:'當時未登錄符合設定的本地服務',
    cloud_free_trial_not_explicitly_authorized:'當時沒有明示允許此雲端免費試用路徑',
    remaining_task_time_insufficient:'當時任務剩餘時間不足',
    saved_candidate_unverifiable:'這一候選的歷史證據無法確認，選擇關係維持未知',
    saved_reason_unknown:'已保存原因不在可辨識清單，內容維持未知'
  };
  const historyReason = value => labelFrom(historyReasons,value);
  const estimateAmount = value => typeof value === 'string' && /^-?(?:\d+(?:\.\d+)?|\.\d+)$/.test(value) ? '$'+escape(value) : '未知';
  const interval = value => record(value) ? `${estimateAmount(value.lower)} ～ ${estimateAmount(value.upper)}` : '未知';
  const configText = value => {
    value = record(value) ? value : {};
    return `<span class="mono">${knownText(value.model_version)}</span><br><span class="metadata muted">推理強度：${knownText(value.effort)} · 供應商：${knownText(value.provider)} · 量化：${knownText(value.quantization)}</span>`;
  };
  function historicalCandidates(candidates) {
    if(!Array.isArray(candidates)||!candidates.length) return '';
    const states={health_recorded:'原健康證據已保存，選擇關係未知',dispatched:'已派送',excluded_before_dispatch:'派送前排除',considered_not_selected:'當時考慮但未選',unknown:'證據未知'};
    return `<div class="table-wrap"><table class="trace-call-table"><thead><tr><th>當時候選設定</th><th>狀態</th><th>當時原因</th><th>原健康觀測</th></tr></thead><tbody>${candidates.filter(record).map(candidate=>{
      const health=record(candidate.health)?candidate.health:{};
      const reasons=Array.isArray(candidate.reasons)?candidate.reasons:[];
      return `<tr><td>${tierName(candidate.tier)} · <code>${knownText(candidate.config_id)}</code><br>${configText(candidate.configuration)}</td><td>${labelFrom(states,candidate.state)}</td><td>${reasons.length?reasons.map(reason=>`${historyReason(reason)}<br><code>${knownText(reason)}</code>`).join('<br>'):'原紀錄未列排除原因'}</td><td>${time(health.observed_at)}<br><span class="metadata muted">快照：<code>${knownText(health.snapshot_id)}</code></span>${health.cooldown_until?`<br>原冷卻期限：${time(health.cooldown_until)}`:''}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  }
  function historicalEconomics(stage) {
    const economics=record(stage.economics)?stage.economics:{};
    if(!['available','partial'].includes(economics.status))return `<p class="metadata muted">完整流程估計：${economics.status==='not_applicable'?'此階段不適用':'未提供可安全對應的完整比較資料'}。分類實付 API 費用：${estimateAmount(stage.classifier_api_usd)}。</p>`;
    const paths=(value)=>record(value)?Object.entries(value).filter(([tier])=>knownTier(tier)).map(([tier,value])=>`${tierName(tier)} ${interval(value)}`).join('；'):'未知';
    return `<dl class="trace-facts"><div><dt>直接路徑完整流程估計 · USD</dt><dd>${paths(economics.direct_paths_usd)}</dd></div><div><dt>分類後完整流程估計 · USD</dt><dd>${paths(economics.classified_paths_usd)}</dd></div><div><dt>分類費估計 · USD</dt><dd>${interval(economics.classifier_fee_estimate_usd)}</dd></div><div><dt>保守淨節省估計 · USD</dt><dd>${estimateAmount(economics.net_saving_lower_usd)}</dd></div><div><dt>分類實付 API 費用 · USD</dt><dd>${estimateAmount(stage.classifier_api_usd)}</dd></div><div><dt>原分類校準版本</dt><dd><code>${knownText(economics.calibration_version)}</code></dd></div><div><dt>原校準證據指紋</dt><dd><code>${knownText(economics.calibration_evidence_sha256)}</code></dd></div><div><dt>原分類協定指紋</dt><dd><code>${knownText(economics.classifier_contract_sha256)}</code></dd></div></dl><p class="metadata muted">以上區間是原完整流程估計，實付沿用原核帳，不重複加總。分類分布不是答對機率。</p>`;
  }
  function historySection(task) {
    const envelope=record(task.operator_observation)?task.operator_observation:{};
    const history=envelope.schema_version==='mrc-observation-v1'&&record(envelope.task_decision)?envelope.task_decision:{};
    const binding=record(history.binding)?history.binding:{};
    const statuses={available:'已保存歷史證據',partial:'部分歷史證據',unknown:'歷史證據未知',not_provided:'未提供歷史觀測'};
    const stages=Array.isArray(history.stages)?history.stages.filter(record):[];
    const costs=record(history.costs)?history.costs:{};
    const triggers={initial:'原任務首答',availability_failure:'供應商可用性故障後的同池備援',public_validation_failed:'公開驗收未通過後的品質補救',public_validation_unavailable:'公開驗收未知後的品質補救'};
    return `<section class="trace-section" data-history-observation><div class="trace-section-title"><h3>任務當時的決策依據</h3>${badge(labelFrom(statuses,history.status),history.status==='available'?'blue':'amber')}</div><p>${historyReason(history.reason)}</p><p class="metadata muted">這是當時的凍結紀錄，不使用現在健康重算。合成或研究證據仍限原範圍；觀測不授予新的模型資格或派送權限。</p><dl class="trace-facts"><div><dt>原候選版本</dt><dd><code>${knownText(binding.pool_version)}</code></dd></div><div><dt>原類別與模式</dt><dd>${knownText(binding.category)}<br>${knownText(binding.mode)}</dd></div><div><dt>原契約</dt><dd>${knownText(binding.contract_id)}<br>${knownText(binding.contract_version)}</dd></div><div><dt>原路由規則與綁定時間</dt><dd>${knownText(binding.routing_version)}<br>${time(binding.bound_at)}</dd></div><div><dt>已知 API 實付 · USD</dt><dd>${estimateAmount(costs.known_api_usd)}</dd></div><div><dt>完整 API 實付 · USD</dt><dd>${estimateAmount(costs.complete_api_usd)}</dd></div><div><dt>客戶用量費</dt><dd>依原費率另列於客戶帳務</dd></div><div><dt>可強制費用上界</dt><dd>未提供可安全對應的完整上界</dd></div></dl>${stages.length?stages.map(stage=>`<article class="trace-decision" data-history-stage><h4>${stage.stage==='low_mid'?'低／中階段':stage.stage==='mid_high'?'中／高階段':'首答與同池候選'}</h4><p><strong>${stage.state==='not_entered'?'未進入此階段':stage.state==='unknown'?'證據未知':labelFrom(triggers,stage.trigger)}</strong></p><p>${historyReason(stage.reason)} <code>${knownText(stage.reason)}</code></p>${stage.state==='recorded'?`<p>原決策：${stage.action==='classify'?'呼叫 Jev 分類':stage.action==='direct'?'直接選模':'停止派送'} · 實際選池：${tierName(stage.selected_tier)}</p>`:''}${historicalEconomics(stage)}${historicalCandidates(stage.candidates)}</article>`).join(''):'<p class="trace-empty">未提供逐候選歷史證據；已有作答與分類紀錄繼續列於下方，缺少紀錄不代表未呼叫。</p>'}</section>`;
  }
  function featuresSection(task) {
    const feature=task.operator_observation?.task_features || task.task_features;
    const valid=record(feature)&&feature.schema_version==='creation-task-features-v1'&&feature.status==='available';
    const key='creation-features-'+encodeURIComponent(JSON.stringify([task.client??'',task.run??'']));
    const labels={input_utf8_bytes:'輸入文字大小',input_codepoints:'輸入字元數',input_tokens:'精確輸入 token',
      original_category:'原 canonical 類別',original_mode:'原模式',messages_count:'訊息數',reference_blocks_count:'參考區塊數',
      output_schema:'輸出 schema 要求',modality_requirements:'模態要求',deadline_requirements:'執行期限要求',
      fee_requirements:'整體任務費用要求',input_modality:'已接受的輸入型態',tools_required:'必要工具能力',
      context_requirements:'明示上下文需求',data_location_policy:'資料位置政策',runtime_limits:'原執行上限'};
    const kinds={explicit:'明示',deterministic:'確定性量測',heuristic:'啟發式提示',unknown:'未知',not_applicable:'不適用',unsupported:'未支援'};
    const units={utf8_bytes:'UTF-8 bytes',unicode_codepoints:'Unicode codepoints',tokens:'tokens',category:'canonical 類別',
      mode:'模式',messages:'訊息',reference_blocks:'參考區塊',schema:'schema',modalities:'模態',seconds:'秒',usd:'USD',
      modality:'輸入型態',boolean:'是否需要',context_tokens:'需求 tokens',location_policy:'執行位置限制',runtime_limits:'契約上限'};
    const sources={accepted_request:'已接受原請求',trusted_binding:'原任務綁定',frozen_requirements:'凍結能力要求',
      frozen_contract:'原受控契約',not_provided:'未提供'};
    const reasons={measured_original_text:'直接量測當時的文字，未估算 tokens',exact_tokenizer_not_provided:'未提供精確 tokenizer、序列化及模型版本，維持未知',
      original_binding:'保留原類別與模式，不推定任務難度',structured_messages_not_provided:'原請求沒有結構化訊息欄位，不能推定一輪對話',
      structured_references_not_provided:'原請求沒有結構化參考區塊，不能推定零份文件',output_schema_not_provided:'受控契約未明示輸出 schema；提示文字不能建立此要求',
      required_modalities_not_provided:'未提供受控模態要求，文字表示不等於完整模態需求',execution_deadline_not_provided:'未明示執行期限；排隊等待期限另列',
      whole_task_fee_requirement_not_provided:'未提供完整任務費用上界；每次呼叫上界、客戶費率與實付分開',plain_text_request:'此入口接受文字 prompt，未解析文件或其他模態',
      required_capabilities_only:'只讀原能力要求；允許工具清單不等於必需工具',frozen_requirements_not_provided:'原契約未提供這項明示要求',
      required_context_not_measured_usage:'這是需求 tokens，並非本次量測或上游實際用量',original_execution_policy:'保留原執行位置限制，不公開私人來源位置',
      caps_are_not_requirements_or_measured_usage:'執行上限與明示需求、實際 tokens 分開'};
    function value(item) {
      if(item.kind==='unknown'||item.value==null)return '未知';
      if(item.id==='tools_required')return item.value===true?'是':item.value===false?'否':'未知';
      if(item.id==='input_modality')return item.value==='text'?'文字':'未知';
      if(item.id==='data_location_policy')return item.value==='local_only'?'原來源同一地點':item.value==='unrestricted'?'原政策未限同一地點':'未知';
      if(item.id==='runtime_limits'){
        const names={max_context_bytes:'上下文 UTF-8 bytes',max_tokens:'每回合輸出 tokens',max_tool_calls:'工具次數',max_turns:'回合數'};
        return record(item.value)?Object.entries(names).map(([field,label])=>`${label}：${quantity(item.value[field])}`).join('<br>'):'未知';
      }
      if(['original_category','original_mode'].includes(item.id))return knownText(item.value);
      return quantity(item.value);
    }
    const items=valid&&Array.isArray(feature.features)?feature.features.filter(item=>record(item)&&Object.hasOwn(labels,item.id)).slice(0,16):[];
    return `<section class="trace-section" data-task-features><div class="trace-section-title"><h3>任務建立時特徵</h3>${badge(valid?'當時紀錄':'無法確認',valid?'blue':'amber')}</div><p>僅供觀察，不改原類別、模型、路由或費用。</p>${!valid?'<p class="trace-empty">當時特徵未提供或無法確認；不回讀原文補算，也不推定曾經停用。</p>':`<dl class="trace-facts"><div><dt>特徵規則版本</dt><dd><code>${knownText(feature.feature_version)}</code></dd></div><div><dt>資料使用政策版本</dt><dd><code>${knownText(feature.data_policy_version)}</code></dd></div><div><dt>原接受時間</dt><dd>${time(feature.captured_at)}</dd></div></dl>${feature.feature_version==='local-deterministic-v1'?'<p class="metadata muted">此版本只記錄三項量測；其他欄位未提供，不用新規則補算。</p>':''}<details data-observation-detail="${escape(key)}"><summary id="${escape(key)}-summary">查看單位、來源與未知原因</summary><div class="table-wrap"><table class="trace-call-table"><thead><tr><th>特徵</th><th>值</th><th>取得類型</th><th>單位</th><th>來源與原因</th></tr></thead><tbody>${items.map(item=>`<tr><td>${escape(labels[item.id])}</td><td>${value(item)}</td><td>${labelFrom(kinds,item.kind)}</td><td>${labelFrom(units,item.unit)}</td><td>${labelFrom(sources,item.source)}<details data-observation-detail="${escape(key+'-'+item.id)}" data-feature-reasons="${escape(item.id)}"><summary id="${escape(key+'-'+item.id)}-reason">查看原因</summary>${Array.isArray(item.reasons)?item.reasons.slice(0,4).map(reason=>`<p>${labelFrom(reasons,reason)}</p>`).join(''):'未知原因'}</details></td></tr>`).join('')}</tbody></table></div></details>`}</section>`;
  }
  // Saved HTTP projection only; no recommendation
  // calculation, source reads, fetches, adoption action or routing controls.
  const recommendationStatuses = {
    available:'已保存建議',unknown:'未知：現有證據不足以判斷',
    not_provided:'未提供：此任務沒有可確認的建議紀錄',
    not_applicable:'不適用：原模式或建立時條件不需此比較',
    unsupported:'未支援：目前不能解讀此類建議資料',
    unavailable:'無法取得：建議紀錄目前無法確認',
    unconfirmed:'未確認：不能確認原建議證據完整'
  };
  const recommendationKinds = {
    observe_original_route:'沿原路由觀察',
    compare_skipping_classification:'可比較略過分類',
    need_capability_context_evidence:'需補能力與上下文證據'
  };
  const recommendationNotes = {
    observe_original_route:'沿原流程觀察。只有已保存的成本比較才能解讀為估計；缺少證據不代表分類無效或不划算。',
    compare_skipping_classification:'僅建議在已保存的條件範圍內比較略過分類；尚未採用、略過分類或改派。',
    need_capability_context_evidence:'依下列原因區分缺少資料、無法比對與已知不相符；本建議不重新批准或排除原候選。'
  };
  // Fixed safe codes observed in the frozen synthetic HTTP projections.
  const recommendationReasons = {
    creation_recommendation_not_provided:'未提供當時的建議紀錄；不能從現在設定推定當時曾停用。',
    creation_recommendation_evidence_unavailable:'建立時建議的保存證據目前無法確認，不補算原建議。',
    exact_classifier_calibration_missing:'缺少完整精確的分類校準，無法確認支付分類費是否有利。',
    exact_classifier_calibration_unverified:'現有校準尚無法確認適用性，無法判定分類是否划算。',
    original_mode_has_no_classification:'原模式沒有分類流程，此分類比較不適用；原模式與呼叫紀錄仍保留。',
    exact_input_tokens_unknown:'本次精確輸入 tokens 未知；不能以字元數估成實際用量或推定模型上下文足夠。',
    declared_context_is_not_measured_usage:'明示上下文需求不是本次實測用量，也不保證整件任務能放入模型上下文。',
    classification_proven_not_economic:'在已保存的完整精確條件及成本估計範圍內，分類淨節省不足；這是可比較略過分類的建議，未採用。',
    conditional_saving_estimated_positive:'保存條件下的分類淨節省估計為正；不是已實現節費，也不改原路由。',
    conditional_saving_interval_uncertain:'保存的淨節省區間仍不確定，不能確認值得略過分類，沿原流程觀察。',
    exact_calibration_source_unavailable:'精確校準來源目前無法安全取得或核對，成本比較維持未知；不顯示私人來源位置。',
    exact_calibration_bounds_unsupported:'校準資料超出已支援的範圍或大小，不能把截取的一部分當完整比較。',
    exact_classifier_calibration_expired_at_acceptance:'原接受時間已超出校準效期，不能據此判定分類是否划算。',
    exact_classifier_calibration_later_than_acceptance:'校準資料晚於原接受時間，不能倒用後來證據形成建立時建議。',
    exact_calibration_scope_or_cost_invalid:'校準範圍或完整成本目前無法核對，成本比較維持未知。',
    classification_flow_time_exceeds_original_limit:'分類流程超出原契約的執行時間上限，不能據此形成成本比較。',
    exact_calibration_source_schema_unsupported:'校準來源種類未支援，未讀取該來源，不能據此形成成本比較。',
    candidate_evidence_expired_at_acceptance:'原接受時間已超出候選證據的快照、目錄、資格或來源效期，成本比較維持未知。',
    candidate_evidence_later_than_acceptance:'候選證據晚於原接受時間，不能倒用後來資料形成建立時建議。',
    exact_calibration_execution_contract_mismatch:'校準對應的執行或驗收版本與本次註冊契約不同，不能據此比較成本。',
    exact_calibration_execution_adapter_unsupported:'此執行核心尚無可確認的成本校準對照方式，成本比較維持未知。'
  };
  const recommendationUIReasons = {
    ui_recommendation_not_provided:'未提供當時的建議紀錄；不能從現在設定推定當時曾停用。',
    ui_recommendation_schema_unsupported:'目前無法解讀此版本或操作員建議資料，保留原任務其他已知明細。',
    ui_recommendation_evidence_unconfirmed:'建議欄位、成本比較或未採用標記無法確認，不推定原建議內容。',
    ui_recommendation_reason_unrecognized:'此原因尚無可辨識的白話說明；不顯示未核對的原始文字。',
    ui_recommendation_state_not_applicable:'此狀態不適用分類比較；未提供更多已保存原因。',
    ui_recommendation_state_not_provided:'此狀態未提供建議，不從後來設定補算當時紀錄。',
    ui_recommendation_state_unsupported:'此狀態尚未支援，不能推定比較已成立。',
    ui_recommendation_state_unconfirmed:'此狀態的建立時證據尚未確認。',
    ui_recommendation_state_unknown:'現有資料不足以判斷，未提供更多已保存原因。'
  };
  const recommendationEconomics = {
    unknown:'分類成本比較未知，不能據此宣稱分類不划算',
    proven_insufficient:'保存條件下的分類淨節省不足（估計）',
    estimated_positive:'保存條件下的分類淨節省估計為正；未採用',
    uncertain:'分類淨節省區間不確定，不改原流程'
  };
  const recommendationSources = {
    offline_fixture:'離線合成校準',observed_calibration:'觀測校準資料（來源種類標記）'
  };
  const recommendationVersions = {
    rule_version:{'route-recommendation-rules-v1':'route-recommendation-rules-v1'},
    data_policy_version:{'creation-recommendation-data-v1':'creation-recommendation-data-v1'}
  };
  const ownCode = (labels,value) => typeof value==='string' && Object.hasOwn(labels,value);
  const recommendationHash = value => typeof value==='string' && /^[a-f0-9]{64}$/.test(value);
  const recommendationReason = value => ownCode(recommendationReasons,value)
    ?{code:value,text:recommendationReasons[value]}
    :{code:'ui_recommendation_reason_unrecognized',text:recommendationUIReasons.ui_recommendation_reason_unrecognized};
  const recommendationDecimal = value => typeof value==='string'&&value.length<=128
    &&/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value);
  function recommendationInterval(value) {
    if(!record(value)||!recommendationDecimal(value.lower)||!recommendationDecimal(value.upper))return false;
    // Exact bounded comparison only; never recompute the saved estimate.
    const decimal=amount=>{
      const fraction=amount.split('.')[1]||'';
      return {integer:BigInt(amount.replace('.','')),scale:fraction.length};
    };
    const lower=decimal(value.lower),upper=decimal(value.upper),scale=Math.max(lower.scale,upper.scale);
    return lower.integer*10n**BigInt(scale-lower.scale)<=upper.integer*10n**BigInt(scale-upper.scale);
  }
  function recommendationComparison(value) {
    return record(value)&&ownCode(recommendationSources,value.source_kind)
      &&Number.isSafeInteger(value.conditional_scopes)&&value.conditional_scopes>0&&value.conditional_scopes<=128
      &&value.distribution_is_answer_probability===false&&recommendationInterval(value.net_saving_usd)
      &&Array.isArray(value.source_sha256)&&value.source_sha256.length>0&&value.source_sha256.length<=16
      &&value.source_sha256.every(recommendationHash);
  }
  function recommendationSection(task) {
    // A present operator envelope is authoritative, including absence or bad data.
    // Never use a root copy to hide a malformed operator envelope/projection.
    const operatorPresent=Object.hasOwn(task,'operator_observation');
    const envelope=operatorPresent?task.operator_observation:null;
    const malformedOperator=operatorPresent&&(!record(envelope)||envelope.schema_version!=='mrc-observation-v1');
    const present=operatorPresent?!malformedOperator&&Object.hasOwn(envelope,'routing_recommendation')
      :Object.hasOwn(task,'routing_recommendation');
    const saved=operatorPresent?(malformedOperator?undefined:envelope.routing_recommendation):task.routing_recommendation;
    let status='not_provided',uiReason='ui_recommendation_not_provided',valid=false,rows=[];
    if(malformedOperator){status='unsupported';uiReason='ui_recommendation_schema_unsupported';}
    else if(present){
      const supported=record(saved)&&saved.schema_version==='creation-routing-recommendation-v1';
      if(!supported){status='unsupported';uiReason='ui_recommendation_schema_unsupported';}
      else if(saved.adopted!==false||!ownCode(recommendationStatuses,saved.status)){
        status='unconfirmed';uiReason='ui_recommendation_evidence_unconfirmed';
      }else{
        status=saved.status;
        const compactReason={not_provided:'creation_recommendation_not_provided',unavailable:'creation_recommendation_evidence_unavailable'};
        const compact=ownCode(compactReason,status)&&saved.reason===compactReason[status]
          &&Object.keys(saved).length===4&&saved.suggestions===undefined;
        const full=['available','unknown','not_applicable','unsupported'].includes(status)
          &&typeof saved.captured_at==='number'&&Number.isFinite(saved.captured_at)&&saved.captured_at>=0
          &&recommendationHash(saved.feature_sha256)&&recommendationHash(saved.context_sha256)
          &&recommendationHash(saved.input_sha256)
          &&ownCode(recommendationVersions.rule_version,saved.rule_version)
          &&ownCode(recommendationVersions.data_policy_version,saved.data_policy_version)
          &&Array.isArray(saved.suggestions)&&saved.suggestions.length<=3
          &&(saved.suggestions.length>0||!['available','unknown'].includes(status))
          &&new Set(saved.suggestions.map(row=>record(row)?row.kind:null)).size===saved.suggestions.length
          &&saved.suggestions.every(row=>record(row)&&ownCode(recommendationKinds,row.kind)
            &&ownCode(recommendationEconomics,row.economics)
            &&Array.isArray(row.reasons)&&row.reasons.length>0&&row.reasons.length<=4
            &&row.reasons.every(reason=>typeof reason==='string'&&/^[a-z][a-z0-9_]{0,79}$/.test(reason))
            &&(!Object.hasOwn(row,'comparison')||recommendationComparison(row.comparison))
            &&(row.economics==='unknown'||recommendationComparison(row.comparison))
            &&(row.kind!=='compare_skipping_classification'||row.economics==='proven_insufficient'));
        valid=compact||full;
        if(valid){uiReason=null;rows=compact?[]:saved.suggestions;}
        else{status='unconfirmed';uiReason='ui_recommendation_evidence_unconfirmed';}
      }
    }
    const identity=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(value);
    const key=identity(task.client)&&identity(task.run)
      ?'routing-recommendation-'+encodeURIComponent(JSON.stringify([task.client,task.run])):null;
    const reasonList=reasons=>`<ul>${reasons.map(reason=>`<li><p>${escape(reason.text)} <code>${escape(reason.code)}</code></p></li>`).join('')}</ul>`;
    const reasonDetail=(suffix,reasons)=>{
      const content=reasonList(reasons);
      if(!key)return `<p class="metadata muted">${reasons.length} 項原因</p>${content}`;
      const detailKey=key+'-'+suffix;
      return `<details data-observation-detail="${escape(detailKey)}" data-recommendation-reasons="${escape(suffix)}"><summary id="${escape(detailKey)}-summary">${ownCode(recommendationKinds,suffix)?escape(recommendationKinds[suffix]):'建議狀態'}：查看原因（${reasons.length} 項）</summary>${content}</details>`;
    };
    const comparisonHTML=row=>{
      if(!Object.hasOwn(row,'comparison'))return '';
      const value=row.comparison,amount=value.net_saving_usd;
      const fingerprints=value.source_sha256.map(hash=>`<code>${escape(hash)}</code>`).join('<br>');
      const sourceKey=key?key+'-'+row.kind+'-comparison-source':null;
      const sourceDetails=sourceKey?`<details data-observation-detail="${escape(sourceKey)}"><summary id="${escape(sourceKey)}-summary">查看校準來源摘要</summary>${fingerprints}</details>`:fingerprints;
      const note=value.source_kind==='offline_fixture'
        ?'合成校準只支持此離線情境的規則與算術，不代表真實節費。'
        :'來源種類標記不證明真實成效；需另核對原資料來源與執行環境。使用此格式的合成資料仍是合成證據。';
      return `<dl class="trace-facts" data-recommendation-comparison><div><dt>估計來源</dt><dd>${escape(recommendationSources[value.source_kind])}<br><code>${escape(value.source_kind)}</code></dd></div><div><dt>條件範圍數</dt><dd>${value.conditional_scopes}</dd></div><div><dt>分類淨節省區間 · USD</dt><dd><code>${escape(amount.lower)}</code> 至 <code>${escape(amount.upper)}</code></dd></div><div><dt>已保存來源指紋</dt><dd>${sourceDetails}</dd></div></dl><p class="metadata muted">${note} 這是保存條件下的完整 API 成本比較估計，不是本任務實付差額。分類分布不是答案通過機率；條件範圍細項未在此摘要提供，不擴大其適用範圍。</p>`;
    };
    const version=field=>ownCode(recommendationVersions[field],saved?.[field])
      ?escape(recommendationVersions[field][saved[field]]):'未提供可辨識版本';
    const metadata=valid&&rows.length?`<dl class="trace-facts"><div><dt>建議規則版本</dt><dd><code>${version('rule_version')}</code></dd></div><div><dt>資料使用政策版本</dt><dd><code>${version('data_policy_version')}</code></dd></div><div><dt>原接受時間</dt><dd>${time(saved.captured_at)}</dd></div><div><dt>對應特徵指紋</dt><dd><code>${escape(saved.feature_sha256)}</code></dd></div></dl>`:'';
    const stateCode=Object.hasOwn(recommendationUIReasons,'ui_recommendation_state_'+status)
      ?'ui_recommendation_state_'+status:'ui_recommendation_state_unconfirmed';
    const emptyReason=uiReason?{code:uiReason,text:recommendationUIReasons[uiReason]}
      :valid&&ownCode(recommendationReasons,saved.reason)?recommendationReason(saved.reason)
      :{code:stateCode,text:recommendationUIReasons[stateCode]};
    return `<section class="trace-section" data-routing-recommendation><div class="trace-section-title"><h3>建立時路由建議</h3>${badge(valid?'建議／未採用':'建議紀錄待確認',valid?'blue':'amber')}</div><p><strong>${escape(recommendationStatuses[status])}</strong></p><p>僅查看建立時保存的觀察性建議。此區不提供採用或改派操作，原類別、候選、模型、路由與費用沿原任務紀錄。</p>${metadata}${rows.length?rows.map(row=>`<article class="trace-decision" data-recommendation-kind="${escape(row.kind)}"><div class="trace-attempt-heading"><h4>${escape(recommendationKinds[row.kind])}</h4>${badge('未採用','blue')}</div><p>${escape(recommendationNotes[row.kind])}</p><p>成本比較：${escape(recommendationEconomics[row.economics])} <code>${escape(row.economics)}</code></p>${comparisonHTML(row)}${reasonDetail(row.kind,row.reasons.map(recommendationReason))}</article>`).join(''):reasonDetail('state',[emptyReason])}<p class="metadata muted">建議不代表已驗證的真實節費、答案通過率或模型資格。精確 tokens、能力需求與原執行上限各自解讀；合成校準僅限其合成範圍。</p></section>`;
  }

  function render(task) {
    task = record(task) ? task : {};
    const trace = record(task.trace) ? task.trace : {}, ingress = record(task.ingress) ? task.ingress : {};
    const attempts = Array.isArray(trace.attempts) ? trace.attempts.filter(record) : [];
    const decisions = Array.isArray(trace.decisions) ? trace.decisions.filter(record) : [];
    return `<div class="task-trace" data-task-trace><div class="trace-header"><div><h3>呼叫與選池明細</h3><p>API 呼叫費用與客戶扣款分開。未知費用保留待核，<a href="#billing">查看客戶帳務 →</a></p></div><span class="badge">不含提示與答案</span></div>${featuresSection(task)}${recommendationSection(task)}${historySection(task)}<section class="trace-section trace-ingress"><h3>客戶端入口</h3><dl class="trace-facts"><div><dt>入口協定</dt><dd>${ingress.protocol === 'ollama' ? 'Ollama' : '未知'}</dd></div><div><dt>請求端點</dt><dd class="mono">${knownText(ingress.endpoint)}</dd></div><div><dt>模型別名</dt><dd class="mono">${knownText(ingress.alias)}</dd></div><div><dt>接收時間</dt><dd>${time(ingress.received_at)}</dd></div></dl><p class="metadata muted">入口的模型別名與各次呼叫實際使用的模型分開列示。</p></section><section class="trace-section"><div class="trace-section-title"><h3>四池嘗試紀錄</h3><span class="metadata muted">開源集合可與品質分組重疊</span></div>${poolCards(task,trace,attempts,decisions)}</section>${attemptsSection(trace,attempts)}${decisionsSection(trace,decisions)}</div>`;
  }
  return {render};
})();
