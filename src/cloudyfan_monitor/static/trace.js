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
    if(economics.status!=='available')return `<p class="metadata muted">完整流程估計：${economics.status==='not_applicable'?'此階段不適用':'未提供可安全對應的完整比較資料'}。分類實付 API 費用：${estimateAmount(stage.classifier_api_usd)}。</p>`;
    const paths=(value)=>record(value)?Object.entries(value).filter(([tier])=>knownTier(tier)).map(([tier,value])=>`${tierName(tier)} ${interval(value)}`).join('；'):'未知';
    return `<dl class="trace-facts"><div><dt>直接路徑完整流程估計 · USD</dt><dd>${paths(economics.direct_paths_usd)}</dd></div><div><dt>分類後完整流程估計 · USD</dt><dd>${paths(economics.classified_paths_usd)}</dd></div><div><dt>分類費估計 · USD</dt><dd>${interval(economics.classifier_fee_estimate_usd)}</dd></div><div><dt>保守淨節省估計 · USD</dt><dd>${estimateAmount(economics.net_saving_lower_usd)}</dd></div><div><dt>分類實付 API 費用 · USD</dt><dd>${estimateAmount(stage.classifier_api_usd)}</dd></div><div><dt>原分類校準版本</dt><dd><code>${knownText(economics.calibration_version)}</code></dd></div></dl><p class="metadata muted">以上區間是原完整流程估計，實付沿用原核帳，不重複加總。分類分布不是答對機率。</p>`;
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
  function render(task) {
    task = record(task) ? task : {};
    const trace = record(task.trace) ? task.trace : {}, ingress = record(task.ingress) ? task.ingress : {};
    const attempts = Array.isArray(trace.attempts) ? trace.attempts.filter(record) : [];
    const decisions = Array.isArray(trace.decisions) ? trace.decisions.filter(record) : [];
    return `<div class="task-trace" data-task-trace><div class="trace-header"><div><h3>呼叫與選池明細</h3><p>API 呼叫費用與客戶扣款分開。未知費用保留待核，<a href="#billing">查看客戶帳務 →</a></p></div><span class="badge">不含提示與答案</span></div>${historySection(task)}<section class="trace-section trace-ingress"><h3>客戶端入口</h3><dl class="trace-facts"><div><dt>入口協定</dt><dd>${ingress.protocol === 'ollama' ? 'Ollama' : '未知'}</dd></div><div><dt>請求端點</dt><dd class="mono">${knownText(ingress.endpoint)}</dd></div><div><dt>模型別名</dt><dd class="mono">${knownText(ingress.alias)}</dd></div><div><dt>接收時間</dt><dd>${time(ingress.received_at)}</dd></div></dl><p class="metadata muted">入口的模型別名與各次呼叫實際使用的模型分開列示。</p></section><section class="trace-section"><div class="trace-section-title"><h3>四池嘗試紀錄</h3><span class="metadata muted">開源集合可與品質分組重疊</span></div>${poolCards(task,trace,attempts,decisions)}</section>${attemptsSection(trace,attempts)}${decisionsSection(trace,decisions)}</div>`;
  }
  return {render};
})();
