'use strict';
window.CloudyFanCapacity = (() => {
  let root = null, context = null, data = null, readState = 'idle', generation = 0, flight = null;
  const h = value => context.esc(value);
  const dimensions = { rpm: 'RPM · 請求數', tpm: 'TPM · token 數', concurrency: '並行數', usd: '美元限制' };
  const statuses = { known: '已記錄', unknown: '未知', unsupported: '未支援', not_applicable: '不適用',
    stale: '已過期', mismatched: '範圍錯配', contradictory: '資料矛盾', unverifiable: '無法確認' };
  const sources = { controlled_import: '受控匯入設定', openrouter_key: 'OpenRouter key 帳務來源', unsupported: '來源尚未支援' };
  const scopes = { account: '帳號', credential: 'credential reference', endpoint: '端點', shared: '共用限額群組' };
  const reasons = {
    supplier_limit_not_provided: '供應商來源沒有提供此限制，不能推定為無限制。',
    local_limit_not_provided: '尚未提供本機保守上限；這不表示供應商容量充足。',
    capacity_unknown: '資料尚未確認，不當作零或可用容量。', capacity_unsupported: '此來源尚未支援這個維度。',
    capacity_not_applicable: '受控設定明示不適用，適用依據保留在觀測中。',
    capacity_clock_invalid: '本次時間無法可信判讀，停止判定資料是否有效。',
    capacity_observation_from_future: '觀測時間晚於目前時間，不能當作目前有效的限制。',
    capacity_evidence_expired: '資料已超過原有效期；保留原觀測時間，不自動更新。',
    capacity_scope_mismatch: '觀測的適用範圍與這個群組不同，停止採用。',
    capacity_reset_time_invalid: '重置時間與原觀測時間矛盾，停止採用。',
    capacity_source_contradictory: '同一維度有互相矛盾的有效資料，停止採用。',
    capacity_source_not_configured: '此 credential reference 尚未接入受控來源。',
    capacity_source_unavailable: '本次來源無法取得，保留先前可信資料與原時間。',
    capacity_refresh_budget_exhausted: '本次更新已達請求數或時間上限，未再發送請求。',
    capacity_refresh_deadline_exceeded: '來源回覆超過本次期限，未採用遲到資料。',
    openrouter_accounting_forbidden: '來源拒絕存取；請由操作員查核伺服器設定。',
    openrouter_accounting_unauthorized: '來源憑證無法通過驗證；不在頁面顯示憑證。',
    openrouter_accounting_rate_limited: '來源要求限流，本頁不自動重試。',
    openrouter_accounting_timeout: '來源查詢逾時，未採用不完整資料。',
    openrouter_accounting_schema_invalid: '來源資料格式無法確認，未採用。',
    openrouter_accounting_identity_mismatch: '來源身分與伺服器綁定不符，未採用。',
    openrouter_accounting_network_failed: '來源連線失敗，本頁不自動重試。',
    openrouter_accounting_response_too_large: '來源回覆超過大小上限，未採用。',
    openrouter_accounting_http_failed: '來源查詢失敗，保留原資料。',
  };
  const reason = code => reasons[code] || '來源資料無法確認，保留安全原因供查核。';
  const stamp = value => value === null || value === undefined ? '未知' : new Date(value * 1000).toLocaleString('zh-TW', { hour12: false });
  const period = value => ({ daily: '每日', weekly: '每週', monthly: '每月' }[value] || '未知');
  const shown = value => value === null || value === undefined ? '未知' : String(value);

  function limit(item, kind) {
    const observations = item.observations || [];
    return `<article class="capacity-limit" data-capacity-dimension="${h(kind + ':' + item.dimension)}"><div class="capacity-limit-title"><strong>${h(dimensions[item.dimension])}</strong><span class="badge ${item.status === 'known' ? 'blue' : 'amber'}">${h(statuses[item.status] || '無法確認')}</span></div><div class="capacity-value"><span>${h(shown(item.value))}</span><small>${h(item.unit)}</small></div><dl><div><dt>原窗口</dt><dd>${item.window_seconds === null ? '未提供固定秒數' : h(item.window_seconds) + ' 秒'}</dd></div><div><dt>適用範圍</dt><dd>${h(scopes[item.scope.kind])} · ${h(item.scope.ref)}</dd></div><div><dt>觀測時間</dt><dd>${h(stamp(item.observed_at))}</dd></div><div><dt>重置時間</dt><dd>${h(stamp(item.reset_at))}${item.reset_period ? ' · ' + h(period(item.reset_period)) : ''}</dd></div><div><dt>有效至</dt><dd>${h(stamp(item.valid_until))}<br>${item.valid_until_basis === "local_freshness" ? "本機資料時效，非供應商保證" : item.valid_until_basis === "controlled_import" ? "受控匯入的適用效期" : "效期依據未知"}</dd></div>${item.dimension === 'usd' ? `<div><dt>原來源剩餘 / 用量</dt><dd>${h(shown(item.remaining))} / ${h(shown(item.usage))} USD</dd></div><div><dt>來源 BYOK inclusion flag</dt><dd>${h(shown(item.include_byok_in_limit))}；不代表外部 BYOK 供應商費用上界</dd></div>` : ''}</dl>${(item.reason_codes || []).map(code => `<p class="capacity-reason">${h(reason(code))}<small class="mono">${h(code)}</small></p>`).join('')}${observations.length ? `<p class="metadata">觀測依據：${observations.map(value => h(value.evidence_ref)).join('、')}</p>` : ''}${item.status !== 'known' && observations.some(value => value.value !== null) ? `<p class="metadata">保留的原始觀測值：${observations.map(value => h(shown(value.value)) + ' ' + h(value.unit)).join('、')}；未當作目前可用量。</p>` : ''}</article>`;
  }

  function group(group) {
    const last = group.last_refresh;
    return `<details class="panel capacity-group" data-capacity-group="${h(group.id)}"><summary><span><strong>${h(group.id)}</strong><small>${h(scopes[group.scope.kind])} · ${h(group.scope.ref)}</small></span><span class="badge">${h(sources[group.source.kind])}</span></summary><div class="capacity-group-body"><p class="metadata">來源版本：<span class="mono">${h(group.source.version)}</span>${group.source.kind === 'controlled_import' ? ' · 由受控設定匯入，未宣稱自動取得' : ''}</p>${last ? `<p class="capacity-refresh-status">最近明示更新：${h(stamp(last.attempted_at))} · ${last.status === 'available' ? '資料已保存' : h(reason(last.error_code))}</p>` : '<p class="metadata">尚未執行明示來源更新。</p>'}${group.retained_after_refresh_failure ? '<p class="capacity-warning">本次更新失敗，保留上次可信資料及原觀測時間；未自動重試。</p>' : ''}<h2>供應商限制</h2><div class="capacity-dimensions">${group.supplier_limits.map(item => limit(item, 'supplier')).join('')}</div><h2>本機保守上限</h2><p class="metadata">本機設定與供應商剩餘量分別保存；本頁不授予派送名額。</p><div class="capacity-dimensions">${group.local_limits.map(item => limit(item, 'local')).join('')}</div>${group.refresh_history?.length ? `<p class="metadata">保存 ${h(group.refresh_history_count)} 次明示更新；本頁列出最近 ${h(group.refresh_history.length)} 次。</p><ul class="capacity-history">${group.refresh_history.map(item => `<li>${h(stamp(item.attempted_at))} · ${item.status === 'available' ? '已保存' : h(reason(item.error_code))} · ${h(item.request_count)} 次來源請求</li>`).join('')}</ul>` : ''}</div></details>`;
  }

  function render() {
    if (!root?.querySelector('#capacity-page')) return;
    const opened = new Set(Array.from(root.querySelectorAll('details[open]')).map(node => node.dataset.capacityGroup || 'bindings'));
    const active = document.activeElement;
    const focusedGroup = active?.tagName === 'SUMMARY' ? active.closest('[data-capacity-group]')?.dataset.capacityGroup : null;
    const focusId = root.contains(active) ? active.id : null;
    const issue = readState === 'stale' ? '<p class="capacity-warning" role="status">本次讀取失敗，保留上次畫面與原始時間。</p>' : readState === 'unavailable' ? '<p class="capacity-warning" role="status">容量資料目前無法讀取；未派送或更新來源。</p>' : '';
    root.innerHTML = `<div id="capacity-page"><div class="page-heading"><div><div class="eyebrow">SUPPLIER CAPACITY</div><h1>供應商限額來源</h1><p>分開查看供應商限制、本機上限及未知資料。來源更新只在操作員明示確認後執行。</p></div><button class="button" id="capacity-refresh-sources" ${!data?.configured ? 'disabled' : ''}>明示更新限額來源</button></div><p class="capacity-warning">${context.demo ? '合成驗收環境 · ' : ''}容量執行政策尚未啟用；保留現有同步派送與滿載拒絕規則。</p>${issue}${!data ? '<div class="panel empty">正在讀取已保存的容量資料…</div>' : !data.configured ? '<div class="panel empty">尚未配置供應商容量來源，沒有自動查詢或可用容量承諾。</div>' : `<p class="metadata">設定版本：<span class="mono">${h(data.configuration_version)}</span></p><section class="panel capacity-bindings" id="capacity-bindings"><h2>伺服器綁定與共用群組</h2><p class="metadata">不同 credential reference 可共用同一帳號限額。頁面不能新增 key、解除群組或擴大上限。</p><div class="table-wrap"><table><thead><tr><th>綁定</th><th>供應商 / 帳號</th><th>Credential reference / 端點</th><th>所有適用群組</th></tr></thead><tbody>${data.bindings.map(binding => `<tr><td class="mono">${h(binding.id)}</td><td>${h(binding.provider)}<br>${h(binding.account_ref)}</td><td>${h(binding.credential_ref)}<br>${h(binding.endpoint_ref)}</td><td>${binding.groups.map(value => `<span class="badge">${h(value)}</span>`).join(' ')}</td></tr>`).join('')}</tbody></table></div></section><div class="capacity-groups">${data.groups.map(group).join('')}</div>`}</div>`;
    root.querySelectorAll('[data-capacity-group]').forEach(node => { node.open = opened.has(node.dataset.capacityGroup); });
    const target = focusedGroup ? Array.from(root.querySelectorAll('[data-capacity-group]')).find(node => node.dataset.capacityGroup === focusedGroup)?.querySelector('summary') : focusId ? document.getElementById(focusId) : null;
    if (focusedGroup || focusId) (target || root.querySelector('#capacity-refresh-sources')).focus({ preventScroll: true });
    root.querySelector('#capacity-refresh-sources').onclick = () => context.confirmAction('明示更新供應商限額來源',
      '<p>使用伺服器配置的來源，在固定時間與請求數上限內查詢 key 限額；不讀取帳號 credits、不發送模型請求，也不修改客戶帳務或派送政策。失敗的來源保留原資料與原時間。</p>',
      async () => { await context.api('/monitor/capacity/refresh', {}); await refresh(); }, '限額來源查核已完成；詳情與未知事項見各群組。');
  }

  function refresh() {
    if (!context || !root?.querySelector('#capacity-page')) return Promise.resolve();
    if (flight) return flight;
    const currentGeneration = generation;
    const pending = (async () => {
      try {
        const next = await context.api('/monitor/capacity');
        if (generation !== currentGeneration || !root?.querySelector('#capacity-page')) return;
        data = next; readState = 'current'; render();
      } catch {
        if (generation !== currentGeneration || !root?.querySelector('#capacity-page')) return;
        readState = data ? 'stale' : 'unavailable'; render();
      } finally { if (flight === pending) flight = null; }
    })();
    flight = pending;
    return pending;
  }

  function mount(node, nextContext) {
    context = nextContext;
    if (root === node && root.querySelector('#capacity-page')) return;
    generation += 1; root = node; data = null; flight = null; readState = 'idle';
    root.innerHTML = '<div id="capacity-page"></div>';
    render(); refresh();
  }
  function resetView() { generation += 1; root = null; context = null; data = null; flight = null; readState = 'idle'; }
  setInterval(() => refresh(), 5000);
  return { mount, refresh, resetView };
})();
