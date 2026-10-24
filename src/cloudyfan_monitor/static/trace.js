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
    const stages = {low_mid:'第一階段 · 低／中分池選擇',mid_high:'第二階段 · 中／高分池選擇'};
    const triggers = {public_validation_failed:'前次公開驗收未通過',public_validation_unavailable:'前次公開驗收無法確認'};
    return `<section class="trace-section"><div class="trace-section-title"><h3>Jev 階段決策</h3><span class="badge amber">分布不是答對機率</span></div>${!Array.isArray(trace.decisions) ? '<p class="trace-empty">Jev 階段紀錄未知，無法推定未呼叫分類。</p>' : !decisions.length ? '<p class="trace-empty">目前沒有已記錄的 Jev 階段決策。</p>' : `<div class="trace-decisions">${decisions.map(decision => `<article class="trace-decision" data-trace-decision><div class="trace-attempt-heading"><h4>${labelFrom(stages,decision.stage)}</h4>${badge(decision.fallback === true ? '已採保守備援選池' : decision.fallback === false ? '未採保守備援選池' : '保守備援狀態未知',decision.fallback === true ? 'amber' : '')}</div><dl class="trace-facts"><div><dt>觸發原因</dt><dd>${labelFrom(triggers,decision.trigger)}</dd></div><div><dt>前次模型池</dt><dd>${tierName(decision.previous_tier)}</dd></div><div><dt>Jev 回報選項</dt><dd>${tierName(decision.reported_choice)}</dd></div><div><dt>實際選池</dt><dd><strong>${tierName(decision.selected_tier)}</strong></dd></div><div><dt>保守備援原因</dt><dd>${decision.fallback === false ? '未啟用' : labelFrom(fallbackLabels,decision.fallback_reason)}</dd></div><div><dt>分類模型</dt><dd class="mono">${knownText(decision.model)}</dd></div><div><dt>分類 API 費用 · USD</dt><dd class="mono">${cost(decision.cost_usd)}</dd></div><div><dt>供應商請求編號</dt><dd><code>${knownText(decision.generation_id)}</code></dd></div></dl><div class="trace-distribution"><h5>Jev 回報分布 · 0–1</h5>${probabilities(decision.probabilities)}<p>此分布用來比較模型池選項，不表示答案通過驗收的機率。</p></div></article>`).join('')}</div>`}</section>`;
  }
  function render(task) {
    task = record(task) ? task : {};
    const trace = record(task.trace) ? task.trace : {}, ingress = record(task.ingress) ? task.ingress : {};
    const attempts = Array.isArray(trace.attempts) ? trace.attempts.filter(record) : [];
    const decisions = Array.isArray(trace.decisions) ? trace.decisions.filter(record) : [];
    return `<div class="task-trace" data-task-trace><div class="trace-header"><div><h3>呼叫與選池明細</h3><p>API 呼叫費用與客戶扣款分開。未知費用保留待核，<a href="#billing">查看客戶帳務 →</a></p></div><span class="badge">不含提示與答案</span></div><section class="trace-section trace-ingress"><h3>客戶端入口</h3><dl class="trace-facts"><div><dt>入口協定</dt><dd>${ingress.protocol === 'ollama' ? 'Ollama' : '未知'}</dd></div><div><dt>請求端點</dt><dd class="mono">${knownText(ingress.endpoint)}</dd></div><div><dt>模型別名</dt><dd class="mono">${knownText(ingress.alias)}</dd></div><div><dt>接收時間</dt><dd>${time(ingress.received_at)}</dd></div></dl><p class="metadata muted">入口的模型別名與各次呼叫實際使用的模型分開列示。</p></section><section class="trace-section"><div class="trace-section-title"><h3>四池嘗試紀錄</h3><span class="metadata muted">開源集合可與品質分組重疊</span></div>${poolCards(task,trace,attempts,decisions)}</section>${attemptsSection(trace,attempts)}${decisionsSection(trace,decisions)}</div>`;
  }
  return {render};
})();
