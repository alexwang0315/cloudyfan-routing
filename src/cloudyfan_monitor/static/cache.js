'use strict';
// 前綴與快取觀測（#46）：只讀既有收據的安全投影；提示不等於快取仍存在，也不改變路由。
window.CloudyFanCache = (() => {
  const hints = {matchable_hint:['可匹配提示','blue'],expired:['超出本機提示窗口','amber'],no_prior_match:['沒有先前相同前綴','amber'],
    not_applicable:['不適用','amber'],unsupported:['供應商未登記支援','amber']};
  const usages = {observed:'回應已回報快取用量',unknown:'回應未回報快取欄位（未知，不填零）',
    not_applicable:'請求未完成或未送出，不能形成熱快取',unsupported:'供應商未登記支援，不使用觀測'};
  const reasons = {identical_leading_messages_in_same_scope:'同一客戶端、同一設定下，開頭訊息逐項完全相同。',
    outside_local_hint_window:'先前相同前綴已超出本機保守提示窗口；不代表供應商快取已失效。',
    no_earlier_sent_request_with_identical_prefix:'沒有更早、已實際送出且有回應的相同前綴請求。',
    provider_cache_support_not_registered:'這個供應商沒有登記相容的快取支援與資料政策。',
    configuration_unmatched:'請求設定無法對應已登記的模型設定。',no_static_prefix:'請求沒有位於任務內容之前的固定訊息。'};
  const cacheResults = {hit:'命中（回應回報讀取量 > 0）',miss:'未命中（回應回報讀取量為 0）',unknown:'未知（沒有回報快取讀取量，不等於未命中）'};
  const apiStates = {authoritative:'帳本權威值',unknown:'費用未知（待核，不填零）',not_recorded:'帳本沒有這筆紀錄'};
  const money = value => value === null || value === undefined ? '未知' : '$' + h(value);
  const taskStates = {available:'可查看',raw_access_expired:'原文存取已到期，不重新開啟',contract_unsupported:'此契約不支援觀測'};
  let root, context, generation = 0, catalog = null, selected = '', detail = null, state = 'idle', notice = '';
  let policy = null, choice = '', applying = false, preparation = null;
  const prepStates = {prepared:'已在付費前準備並凍結',not_requested:'此任務未使用參考區塊',raw_access_expired:'原文存取已到期，不重新開啟',
    not_created:'任務尚未建立',unverifiable:'保存的準備資料無法核對；不重新準備',
    rejected_before_dispatch:'付費前已拒絕；沒有送出任何請求'};
  const outcomes = {prepared:'已整塊選取',no_reduction:'沒有可省略的部分，送出完整原文',fallback_original:'準備逾時，付費前回退完整原文'};
  const rejectionReasons = {required_context_over_limit:'必要內容超出上下文上限',token_method_untrusted:'需要 token 保障但沒有可信計數方法',
    context_preparation_not_qualified:'資格未涵蓋此準備版本',context_preparation_timeout:'準備逾時，且政策不允許回退',
    preparation_unavailable_original_over_limit:'準備逾時，完整原文也放不下',output_reserve_exceeds_context:'框架與輸出預留已超過上下文',
    context_preparation_not_registered:'此契約沒有登記參考區塊政策',context_preparation_version_not_registered:'準備版本未登記'};
  const blockReasons = {required_by_request:'呼叫端標為必要',server_policy_disallows_omission:'伺服器政策不允許省略',
    fits_in_priority_order:'依優先序放得下',remaining_budget_insufficient:'剩餘額度不足，整塊省略'};
  const affinityReasons = {identical_leading_messages_in_same_scope:'等價候選中有一個與先前已送出請求的固定前綴相同。',
    no_earlier_sent_request_with_identical_prefix:'等價候選都沒有可信的相同前綴；沿原排序。',no_equivalent_set:'沒有完全等價的候選；不比較快取。',
    affinity_preference_outside_equivalent_set:'提示指向等價集合之外的候選；不採用，沿原排序。',affinity_port_failed:'提示無法取得；沿原排序。',
    contract_unsupported:'此契約不支援親和性；沿原排序。',
    outside_local_hint_window:'相同前綴已超出本機提示窗口；不宣稱快取仍在，沿原排序。',
    outside_source_ttl:'相同前綴已超出供應商來源 TTL；沿原排序。',
    clock_anomaly:'時鐘異常（先前請求時間晚於現在）；略過親和性，沿原排序。',
    clock_unavailable:'時鐘無法使用；略過親和性，沿原排序。',
    response_model_mismatch:'先前回應的模型與請求不符，不當作熱快取來源；沿原排序。'};
  const validPolicy = value => !!value && value.schema_version === 'cache-affinity-v1' && Number.isSafeInteger(value.revision)
    && (value.active_version === null || typeof value.active_version === 'string') && Array.isArray(value.available_versions)
    && value.available_versions.every(row => typeof row?.version === 'string');
  const h = value => context.esc(value);
  const mounted = () => !!root?.querySelector('#cache-page');
  const identity = row => !!row && typeof row.client === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(row.client)
    && typeof row.run_id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(row.run_id);
  const key = row => row.client + '/' + row.run_id;
  const count = value => value === null || Number.isSafeInteger(value) && value >= 0;
  function validCatalog(value) {
    return !!value && value.schema_version === 'prefix-cache-observation-catalog-v1' && Array.isArray(value.tasks)
      && value.tasks.length <= 100 && value.tasks.every(identity) && typeof value.tasks_truncated === 'boolean'
      && Array.isArray(value.supported_providers) && value.supported_providers.every(row => typeof row?.provider === 'string'
        && count(row.source_ttl_seconds));
  }
  function validDetail(value) {
    return !!value && value.schema_version === 'prefix-cache-observation-v1' && identity(value.task) && value.adopted === false
      && Object.hasOwn(taskStates,value.status) && Array.isArray(value.calls) && value.calls.length <= 32
      && value.calls.every(row => Number.isSafeInteger(row.index) && typeof row.sent === 'boolean'
        && !!row.prefix && count(row.prefix.messages) && count(row.prefix.bytes)
        && (row.prefix.identity === null || /^[a-f0-9]{32}$/.test(row.prefix.identity))
        && !!row.hint && Object.hasOwn(hints,row.hint.status) && count(row.hint.matched_messages) && count(row.hint.source_ttl_seconds)
        && !!row.cache_usage && Object.hasOwn(usages,row.cache_usage.status)
        && count(row.cache_usage.cache_read_tokens) && count(row.cache_usage.cache_write_tokens) && !Object.hasOwn(row,'messages'));
  }
  function callView(row) {
    const hint = hints[row.hint.status], usage = row.cache_usage;
    return `<li class="cache-call" data-cache-call="${h(row.index)}"><div class="cache-columns"><section><h4>前綴提示</h4><span class="badge ${hint[1]}" data-cache-hint="${h(row.hint.status)}">${hint[0]}</span><p class="metadata">${h(reasons[row.hint.reason] || '沒有可用的提示原因。')}</p><p>固定前綴 ${h(row.prefix.messages)} 則訊息 · ${h(row.prefix.bytes)} bytes${row.hint.matched_messages ? ` · 與先前相同 ${h(row.hint.matched_messages)} 則` : ''}</p></section><section><h4>實際快取用量</h4><p data-cache-usage="${h(usage.status)}">${h(usages[usage.status])}</p>${usage.status === 'observed' ? `<p>讀取 ${h(usage.cache_read_tokens ?? '未回報')} tokens · 寫入 ${h(usage.cache_write_tokens ?? '未回報')} tokens</p>` : ''}</section></div>${row.cache_result ? `<dl class="comparison-costs cache-cost" data-cache-cost="${h(row.index)}"><div><dt>快取結果</dt><dd data-cache-result="${h(row.cache_result)}">${h(cacheResults[row.cache_result] || '未知')}${row.cache_usage?.cache_write_tokens ? ` · 寫入 ${h(row.cache_usage.cache_write_tokens)} tokens` : ''}</dd></div><div><dt>Token 用量</dt><dd>輸入 ${h(row.tokens?.prompt_tokens ?? '未回報')} · 輸出 ${h(row.tokens?.completion_tokens ?? '未回報')}</dd></div>${row.api_cost !== undefined ? `<div><dt>API 實付（核帳權威值）</dt><dd data-api-cost="${h(row.api_cost?.status ?? 'none')}">${row.api_cost ? `${money(row.api_cost.amount_usd)} · ${h(apiStates[row.api_cost.status] || '')}${row.api_cost.verified ? ' · 已核帳' : ''}` : '未提供'}</dd></div><div><dt>保守費用上界（限制，不是花費）</dt><dd>${money(row.conservative_bound_usd)}</dd></div>` : ''}</dl>` : ''}<details data-cache-detail="${h(row.index)}"><summary>查看設定與時間</summary><dl class="comparison-facts"><div><dt>模型</dt><dd><code>${h(row.model ?? '未提供')}</code></dd></div><div><dt>供應商</dt><dd>${h(row.provider ?? '未提供')}</dd></div><div><dt>請求開始</dt><dd>${h(row.started_at ?? '未提供')}</dd></div><div><dt>先前相同前綴</dt><dd>${h(row.hint.previous_started_at ?? '無')}</dd></div><div><dt>本機提示窗口</dt><dd>${h(row.hint.local_window_seconds)} 秒（本機保守值，不是供應商 TTL）</dd></div><div><dt>來源 TTL</dt><dd>${row.hint.source_ttl_seconds === null ? '供應商未提供' : h(row.hint.source_ttl_seconds) + ' 秒'}</dd></div><div><dt>前綴身分</dt><dd class="mono">${h(row.prefix.identity ?? '無')}</dd></div><div><dt>已送出</dt><dd>${row.sent ? '是' : '否'}</dd></div></dl></details></li>`;
  }
  function preparationView(value) {
    if (!value || !Object.hasOwn(prepStates,value.status)) return '';
    const p = value.preparation;
    return `<section class="panel cache-preparation" data-preparation-status="${h(value.status)}"><div class="panel-header"><h2>參考區塊準備</h2><span class="badge ${value.status === 'prepared' ? 'blue' : 'amber'}">${h(prepStates[value.status])}</span></div><div class="panel-body">${Array.isArray(value.rejections) ? `<ul>${value.rejections.map(row => `<li data-preparation-rejection="${h(row.code)}">${h(rejectionReasons[row.code] || '輸入格式或範圍不符')}</li>`).join('')}</ul><p class="metadata">拒絕發生在建立任務與付費之前；沒有送出任何請求，也沒有保存原文。</p>` : ''}${p ? `<dl class="comparison-costs"><div><dt>準備版本</dt><dd><code>${h(p.version)}</code> · 只做整塊保留或省略</dd></div><div><dt>結果</dt><dd data-preparation-outcome="${h(p.outcome)}">${h(outcomes[p.outcome] || '未知')}</dd></div><div><dt>長度（${h(p.measure)}）</dt><dd>原始 ${h(p.original_bytes)} · 準備後 ${h(p.prepared_bytes)} · 實際送出 ${h(p.sent_bytes ?? p.prepared_bytes)} bytes · 預算 ${h(p.budget_bytes)}</dd></div><div><dt>Token</dt><dd>本機計數：${p.token_estimate === null || p.token_estimate === undefined ? '未知（沒有可信 tokenizer，不用字元估算）' : h(p.token_estimate)} · 供應商回報輸入：${p.provider_prompt_tokens === null || p.provider_prompt_tokens === undefined ? '未回報' : h(p.provider_prompt_tokens)}</dd></div><div><dt>實際送出與凍結內容</dt><dd data-preparation-sent="${h(String(p.sent_matches_prepared))}">${p.sent_matches_prepared === true ? '一致' : p.sent_matches_prepared === false ? '不一致' : '尚未送出'}</dd></div><div><dt>準備資格</dt><dd>${h(p.qualification.scope)}${p.qualification.synthetic ? ' · 合成接線，不是正式資格' : ''}</dd></div></dl><ol>${p.blocks.map(row => `<li data-preparation-block="${h(row.index)}" data-preparation-decision="${h(row.decision)}">區塊 ${h(row.index + 1)}（<span class="mono">${h(row.id_digest)}</span>）· ${row.decision === 'kept' ? '保留' : '省略'} · ${h(row.bytes)} bytes · 優先序 ${h(row.priority)}<br><span class="metadata">${h(blockReasons[row.reason] || '沒有可用原因。')}</span></li>`).join('')}</ol>${Array.isArray(p.dispatches) && p.dispatches.length ? `<ol class="metadata">${p.dispatches.map(row => `<li data-preparation-dispatch="${h(row.call_index)}" data-dispatch-matches="${h(String(row.matches_prepared))}">第 ${h(row.call_index + 1)} 次生成請求 · ${row.matches_prepared ? '送出內容與凍結內容一致' : '與凍結內容不一致'} · 供應商回報輸入 ${h(row.provider_prompt_tokens ?? '未回報')}</li>`).join('')}</ol>` : ''}<p class="metadata">本機確定性擷取；沒有摘要、改寫或模型呼叫，不宣稱一般語意無損或節費。</p>` : ''}</div></section>`;
  }
  function costView(costs) {
    if (!costs) return '';
    if (costs.status === 'unavailable') return '<p class="comparison-warning">費用明細目前無法確認；未重新計算。</p>';
    const charge = costs.customer_usage_charge;
    return `<section class="panel cache-costs"><div class="panel-header"><h2>費用意義分開呈現</h2></div><div class="panel-body"><dl class="comparison-costs">${costs.api_known_usd !== undefined ? `<div><dt>API 已知實付</dt><dd data-api-known="${h(costs.api_known_usd)}">${money(costs.api_known_usd)}</dd></div><div><dt>API 完整實付</dt><dd>${costs.api_complete_usd === null ? `未定（${h(costs.api_unknown_events)} 筆費用未知）` : money(costs.api_complete_usd)}</dd></div><div><dt>原預留（上界，不是花費）</dt><dd>${costs.reservation ? `${money(costs.reservation.amount_usd)} · ${h(costs.reservation.status)}` : '未提供'}</dd></div>` : ''}<div><dt>客戶用量費（任務接受時固定費率）</dt><dd data-customer-charge="${h(charge?.status ?? 'none')}">${!charge || charge.status === 'not_provided' ? '未提供' : charge.status === 'unknown' ? `待核（已知 ${money(charge.known_usd)}，${h(charge.pending_entries)} 筆待核）` : money(charge.complete_usd)}</dd></div></dl><p class="metadata">實付只取原回應或核帳的權威值一次，核帳取代不加算；不以 token 差、公開牌價或推測快取折扣重算，也不據此折扣、補扣或退款。</p></div></section>`;
  }
  function affinityView(value) {
    if (!value) return '';
    if (value.status === 'unavailable') return '<p class="comparison-warning">親和性紀錄目前無法確認。</p>';
    const rows = Array.isArray(value.decisions) ? value.decisions : [];
    return `<section class="panel cache-affinity-task"><div class="panel-header"><h2>快取親和性決策</h2><span class="badge ${value.frozen_version ? 'blue' : 'amber'}" data-affinity-frozen="${h(value.frozen_version ?? 'none')}">${value.frozen_version ? '任務凍結版本 ' + h(value.frozen_version) : '此任務未採用親和性'}</span></div><div class="panel-body">${rows.length ? `<ol>${rows.map(row => `<li data-affinity-decision="${h(row.decision_key)}"><strong>${h(row.applied?.tier ?? row.decision_key)}</strong> · ${row.applied ? (row.applied.adopted ? '<span data-affinity-adopted="true">已採用次要排序</span>' : '<span data-affinity-adopted="false">未改變原排序</span>') : '尚無 Core 決策'}<br><span class="metadata" data-affinity-reason="${h(row.applied?.reason ?? row.hint?.reason ?? '')}">${h(affinityReasons[row.applied?.reason ?? row.hint?.reason] || '沒有可用原因。')}</span>${row.hint?.unreadable_sources ? `<br><span class="metadata">略過 ${h(row.hint.unreadable_sources)} 個無法核對的先前來源；其他可信來源照用。</span>` : ''}${row.applied ? `<br><span class="metadata">等價候選 ${h((row.applied.equivalent_config_ids || []).join('、'))} · 原排序 <code>${h(row.applied.original_config_id)}</code> · 實際 <code>${h(row.applied.selected_config_id)}</code></span>` : ''}</li>`).join('')}</ol>` : '<p class="metadata">沒有親和性決策；原排序未被改變。</p>'}${Array.isArray(value.sent_models) && value.sent_models.length ? `<p class="metadata">實際送出模型：${value.sent_models.map(row => `<code>${h(row.model)}</code>`).join(' → ')}</p>` : ''}<p class="metadata">只在全部閘門之後、完整成本與速度區間完全相同的候選之間打破平手；不預熱、不探測，也不宣稱實際命中或降本。</p></div></section>`;
  }
  function policyView() {
    if (!policy) return '<p class="comparison-warning" role="status">親和性政策狀態尚未確認。</p>';
    const options = [['','不採用親和性'],...policy.available_versions.map(row => [row.version, row.version])];
    return `<section class="panel cache-affinity-policy"><div class="panel-header"><h2>快取親和性政策</h2><span class="badge ${policy.active_version ? 'blue' : 'amber'}" data-affinity-active="${h(policy.active_version ?? 'none')}">${policy.active_version ? '新任務採用 ' + h(policy.active_version) : '未採用'}</span></div><div class="panel-body"><label for="cache-affinity-version">新任務適用的政策版本</label><select id="cache-affinity-version" ${applying ? 'disabled' : ''}>${options.map(([value,label]) => `<option value="${h(value)}" ${choice === value ? 'selected' : ''}>${h(label)}</option>`).join('')}</select><button id="cache-affinity-apply" class="button primary" type="button" ${applying || choice === (policy.active_version ?? '') ? 'disabled' : ''}>套用到新任務</button><p class="metadata">只影響之後建立的任務；既有、重送與歷史任務保留原凍結版本。</p></div></section>`;
  }
  function render() {
    if (!mounted()) return;
    const rows = catalog?.tasks || [];
    const body = !selected ? '<div class="panel empty">選擇一個任務，查看它實際送出的請求。</div>'
      : state !== 'current' || !detail ? `<p class="comparison-warning" role="status">${h(notice || '正在讀取觀測；尚未確認。')}</p>`
      : `<section class="panel"><div class="panel-header"><h2 class="mono">${h(detail.task.run_id)}</h2><span class="badge blue">${h(taskStates[detail.status])}</span></div><div class="panel-body"><p>客戶端 ${h(detail.task.client)}</p>${detail.calls.length ? `<ol class="cache-calls">${detail.calls.map(callView).join('')}</ol>` : '<p class="metadata">沒有可查看的請求。</p>'}${detail.calls_truncated ? '<p class="comparison-warning">請求數超過顯示上限，只列前段。</p>' : ''}</div></section>${preparationView(preparation)}${costView(detail.costs)}${affinityView(detail.affinity)}`;
    root.querySelector('#cache-page').innerHTML = `<div class="page-heading"><div><div class="eyebrow">PREFIX CACHE OBSERVATION</div><h1>前綴與快取觀測</h1><p>只讀實際已送出請求與回應的安全摘要；提示不保證下次命中，也不改變路由排序。</p></div></div><p class="notice">不新增 cache-control、不改 TTL、不重排訊息、不預熱或探測。供應商未登記支援時只顯示「未登記支援」。${catalog ? ` 已登記支援：${catalog.supported_providers.map(row => h(row.provider)).join('、') || '無'}。` : ''}</p>${context.demo ? '<p class="metadata comparison-synthetic">合成驗收環境 · 不代表真實供應商的快取行為或費用。</p>' : ''}${policyView()}<section class="panel comparison-saved"><div class="panel-body"><label for="cache-task">選擇任務</label><select id="cache-task"><option value="">請明示選擇任務</option>${rows.map(row => `<option value="${h(key(row))}" ${selected === key(row) ? 'selected' : ''}>${h(row.run_id)} · ${h(row.client)}</option>`).join('')}</select></div></section>${body}`;
    root.querySelector('#cache-task').onchange = event => {selected = event.target.value;detail = preparation = null;state = 'loading';notice = '';render();void readDetail();};
    const version = root.querySelector('#cache-affinity-version');
    if (version) version.onchange = event => {choice = event.target.value;render();};
    const apply = root.querySelector('#cache-affinity-apply');
    if (apply) apply.onclick = activate;
  }
  function activate() {
    const actionContext = context, revision = policy?.revision, target = choice || null;
    if (!policy || applying) return;
    actionContext.confirmAction('套用快取親和性政策',`<p>新任務改用 <code>${h(target ?? '不採用')}</code>。</p><p>既有、重送與歷史任務保留原凍結版本；只在等價候選間打破平手，不改變閘門、成本估計或品質。</p>`,async () => {
      applying = true;render();
      try {
        const value = await actionContext.api('/monitor/cache-affinity/activate',{revision,version:target});
        if (!validPolicy(value)) throw Error('cache_affinity_unconfirmed');
        policy = value;choice = value.active_version ?? '';
      } finally {applying = false;render();}
    },'快取親和性政策已套用到新任務。',false,'套用');
  }
  async function readPolicy() {
    const version = generation, actionContext = context;
    try {
      const value = await actionContext.api('/monitor/cache-affinity');
      if (version !== generation || !mounted()) return;
      if (!validPolicy(value)) throw Error('cache_affinity_unconfirmed');
      const first = policy === null;policy = value;if (first) choice = value.active_version ?? '';render();
    } catch {if (version === generation && mounted()) {policy = null;render();}}
  }
  async function readCatalog() {
    const version = generation, actionContext = context;
    try {
      const value = await actionContext.api('/monitor/cache-observations');
      if (version !== generation || !mounted()) return;
      if (!validCatalog(value)) throw Error('cache_catalog_unconfirmed');
      catalog = value;render();
    } catch {if (version === generation && mounted()) {notice = '任務清單目前無法確認。';render();}}
  }
  async function readDetail() {
    const version = generation, actionContext = context, target = selected;
    if (!target) return;
    try {
      const value = await actionContext.api('/monitor/cache-observations/' + target.split('/').map(encodeURIComponent).join('/'));
      if (version !== generation || selected !== target || !mounted()) return;
      if (!validDetail(value) || key(value.task) !== target) throw Error('cache_detail_unconfirmed');
      try {
        const prep = await actionContext.api('/monitor/context-preparations/' + target.split('/').map(encodeURIComponent).join('/'));
        preparation = prep?.schema_version === 'context-preparation-observation-v1' ? prep : null;
      } catch {preparation = null;}
      if (version !== generation || selected !== target || !mounted()) return;
      detail = value;state = 'current';render();
    } catch {if (version === generation && selected === target && mounted()) {state = 'stale';notice = '這個任務的觀測目前無法確認；未重新計算或送出任何請求。';render();}}
  }
  function mount(node, nextContext) {
    // 主頁每次輪詢都會呼叫 mount；同一登入與同一容器時保留選取，只在真正換頁或換身分時重設。
    if (context?.sessionVersion === nextContext.sessionVersion && root === node && mounted()) return;
    generation += 1;root = node;context = nextContext;catalog = detail = null;selected = notice = '';state = 'idle';
    policy = null;choice = '';applying = false;
    root.innerHTML = '<div id="cache-page"></div>';render();void readCatalog();void readPolicy();
  }
  function refresh() {return Promise.all([readPolicy(), selected ? readDetail() : readCatalog()]);}
  return {mount,refresh};
})();
