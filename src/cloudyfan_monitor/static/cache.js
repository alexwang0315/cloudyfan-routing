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
  const taskStates = {available:'可查看',raw_access_expired:'原文存取已到期，不重新開啟',contract_unsupported:'此契約不支援觀測'};
  let root, context, generation = 0, catalog = null, selected = '', detail = null, state = 'idle', notice = '';
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
    return `<li class="cache-call" data-cache-call="${h(row.index)}"><div class="cache-columns"><section><h4>前綴提示</h4><span class="badge ${hint[1]}" data-cache-hint="${h(row.hint.status)}">${hint[0]}</span><p class="metadata">${h(reasons[row.hint.reason] || '沒有可用的提示原因。')}</p><p>固定前綴 ${h(row.prefix.messages)} 則訊息 · ${h(row.prefix.bytes)} bytes${row.hint.matched_messages ? ` · 與先前相同 ${h(row.hint.matched_messages)} 則` : ''}</p></section><section><h4>實際快取用量</h4><p data-cache-usage="${h(usage.status)}">${h(usages[usage.status])}</p>${usage.status === 'observed' ? `<p>讀取 ${h(usage.cache_read_tokens ?? '未回報')} tokens · 寫入 ${h(usage.cache_write_tokens ?? '未回報')} tokens</p>` : ''}</section></div><details data-cache-detail="${h(row.index)}"><summary>查看設定與時間</summary><dl class="comparison-facts"><div><dt>模型</dt><dd><code>${h(row.model ?? '未提供')}</code></dd></div><div><dt>供應商</dt><dd>${h(row.provider ?? '未提供')}</dd></div><div><dt>請求開始</dt><dd>${h(row.started_at ?? '未提供')}</dd></div><div><dt>先前相同前綴</dt><dd>${h(row.hint.previous_started_at ?? '無')}</dd></div><div><dt>本機提示窗口</dt><dd>${h(row.hint.local_window_seconds)} 秒（本機保守值，不是供應商 TTL）</dd></div><div><dt>來源 TTL</dt><dd>${row.hint.source_ttl_seconds === null ? '供應商未提供' : h(row.hint.source_ttl_seconds) + ' 秒'}</dd></div><div><dt>前綴身分</dt><dd class="mono">${h(row.prefix.identity ?? '無')}</dd></div><div><dt>已送出</dt><dd>${row.sent ? '是' : '否'}</dd></div></dl></details></li>`;
  }
  function render() {
    if (!mounted()) return;
    const rows = catalog?.tasks || [];
    const body = !selected ? '<div class="panel empty">選擇一個任務，查看它實際送出的請求。</div>'
      : state !== 'current' || !detail ? `<p class="comparison-warning" role="status">${h(notice || '正在讀取觀測；尚未確認。')}</p>`
      : `<section class="panel"><div class="panel-header"><h2 class="mono">${h(detail.task.run_id)}</h2><span class="badge blue">${h(taskStates[detail.status])}</span></div><div class="panel-body"><p>客戶端 ${h(detail.task.client)}</p>${detail.calls.length ? `<ol class="cache-calls">${detail.calls.map(callView).join('')}</ol>` : '<p class="metadata">沒有可查看的請求。</p>'}${detail.calls_truncated ? '<p class="comparison-warning">請求數超過顯示上限，只列前段。</p>' : ''}</div></section>`;
    root.querySelector('#cache-page').innerHTML = `<div class="page-heading"><div><div class="eyebrow">PREFIX CACHE OBSERVATION</div><h1>前綴與快取觀測</h1><p>只讀實際已送出請求與回應的安全摘要；提示不保證下次命中，也不改變路由排序。</p></div></div><p class="notice">不新增 cache-control、不改 TTL、不重排訊息、不預熱或探測。供應商未登記支援時只顯示「未登記支援」。${catalog ? ` 已登記支援：${catalog.supported_providers.map(row => h(row.provider)).join('、') || '無'}。` : ''}</p>${context.demo ? '<p class="metadata comparison-synthetic">合成驗收環境 · 不代表真實供應商的快取行為或費用。</p>' : ''}<section class="panel comparison-saved"><div class="panel-body"><label for="cache-task">選擇任務</label><select id="cache-task"><option value="">請明示選擇任務</option>${rows.map(row => `<option value="${h(key(row))}" ${selected === key(row) ? 'selected' : ''}>${h(row.run_id)} · ${h(row.client)}</option>`).join('')}</select></div></section>${body}`;
    root.querySelector('#cache-task').onchange = event => {selected = event.target.value;detail = null;state = 'loading';notice = '';render();void readDetail();};
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
      detail = value;state = 'current';render();
    } catch {if (version === generation && selected === target && mounted()) {state = 'stale';notice = '這個任務的觀測目前無法確認；未重新計算或送出任何請求。';render();}}
  }
  function mount(node, nextContext) {
    generation += 1;root = node;context = nextContext;catalog = detail = null;selected = notice = '';state = 'idle';
    root.innerHTML = '<div id="cache-page"></div>';render();void readCatalog();
  }
  function refresh() {return selected ? readDetail() : readCatalog();}
  return {mount,refresh};
})();
