/* Customer accounting: amounts are decimal strings calculated by the server. */
'use strict';
window.CloudyFanBilling = (() => {
  let root = null, context = null, data = null, loading = null, busy = false, mountVersion = 0, requestVersion = 0;
  let dirty = false, settingsRevision = null, rowNumber = 0, creditIntent = null, settingsDraft = null;
  const pendingCreditKey = 'cloudyfan.billing.pending-credit.v1';
  let storageIssue = '', recoveredCredit = false, confirmedCredit = false;
  const h = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const q = selector => root?.querySelector(selector);
  const amount = value => typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value) ? '$' + h(value) : '未知';
  const decimalText = value => typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value) ? h(value) : '未知';
  const tokenCount = value => (typeof value === 'string' && /^\d+$/.test(value)) || (Number.isSafeInteger(value) && value >= 0) ? h(value) : '未知';
  const stamp = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-TW', {hour12:false}) : '尚未取得';
  const reasonLabels = {
    provider_not_configured:'尚未設定帳務來源',source_not_configured:'尚未設定帳務來源',
    missing_model_price:'尚未設定此模型售價',open_source_price_missing:'尚未設定開源模型售價',
    missing_token_usage:'缺少可核對的 token 用量',token_usage_missing:'缺少可核對的 token 用量',
    unknown_cost:'供應商費用待核',cost_unknown:'供應商費用待核',unverified_supplier_cost:'供應商費用待核',
    policy_disabled:'此任務建立時未啟用計費',legacy_task_no_pricing:'舊任務未綁定售價，保留原紀錄',
    pricing_not_frozen:'此任務未綁定售價',model_unknown:'模型身分待核',
    owner_unknown:'客戶歸屬待核',missing_generation_id:'缺少供應商核帳編號',
    generation_not_found:'供應商核帳資料尚未取得',accounting_unavailable:'帳務來源暫時無法取得',
    billing_open_source_price_missing:'尚未設定此開源模型售價',billing_token_usage_pending:'缺少可核對的 token 用量',
    billing_supplier_cost_pending:'供應商實際費用待核',billing_request_metadata_missing:'請求身分或用量資料待核',
    billing_supplier_not_supported:'此供應商尚未接入實際帳務',billing_task_running:'任務仍在執行，待完成後核帳',
    openrouter_accounting_not_configured:'尚未設定 OpenRouter 帳務憑證',billing_configuration_changed_reload:'售價版本已更新，請重新載入設定',
    billing_idempotency_conflict:'此入帳編號已有不同內容，請核對原紀錄',billing_disabled:'此任務未啟用客戶計費',
    billing_reconciliation_busy:'另一筆核帳仍在進行，請稍後讀取原結果',billing_batch_deferred:'本批核帳已達上限，請再次核帳',
    openrouter_accounting_not_found:'OpenRouter 尚未提供這筆核帳資料',openrouter_accounting_identity_mismatch:'供應商回傳的請求或模型身分不符，已停止扣款',
    openrouter_accounting_timeout:'OpenRouter 帳務查詢逾時，原資料保留',openrouter_accounting_forbidden:'目前憑證沒有這項帳務查詢權限',
    billing_invalid_amount:'金額或倍率超出允許範圍，請檢查輸入',billing_note_required:'請填寫入帳備註',
    billing_invalid_configuration:'售價設定格式有誤，請重新讀取後檢查',billing_invalid_model_price:'模型 ID 或售價格式有誤',
    client_not_found:'客戶目前不存在，請核對客戶身分與原帳本',unauthorized:'登入已失效或權限不足，請重新認證'
  };
  const basisLabels = {openrouter_actual_cost:'OpenRouter 實際費用 × 倍率',openrouter_generation_api:'OpenRouter 實際費用 × 倍率',open_source_tokens:'開源模型 token 售價',open_source_token_price:'開源模型 token 售價',
    open_source_price:'開源模型 token 售價',synthetic_core_budget:'合成費用 × 倍率',credit:'客戶入帳'};
  const statusLabels = {available:'已取得',partial:'部分資料可用',unavailable:'尚未取得',not_configured:'未設定'};
  const errorText = value => typeof value === 'string' ? (reasonLabels[value] || value) : (reasonLabels[value?.code] || value?.code || '資料待核');

  function alert(message) {
    const box = q('#billing-alert');
    if(box) { box.hidden = !message; box.textContent = message || ''; }
  }
  function busyControls() {
    if(!root) return;
    root.querySelectorAll('button,input,select,textarea').forEach(element => {
      if(busy) {
        if(!('billingWasDisabled' in element.dataset)) element.dataset.billingWasDisabled = element.disabled ? '1' : '0';
        element.disabled = true;
      } else if('billingWasDisabled' in element.dataset) {
        element.disabled = element.dataset.billingWasDisabled === '1';
        delete element.dataset.billingWasDisabled;
      }
    });
    const indicator = q('#billing-busy');
    if(indicator) indicator.hidden = !busy;
  }
  async function mutate(action) {
    if(busy) throw Error('另一筆帳務操作仍在進行，請等候原結果。');
    busy = true; alert(''); busyControls();
    try { return await action(); }
    catch(error) { alert(errorText(error.message || '帳務操作尚未確認，請保留原紀錄。')); throw error; }
    finally { busy = false; busyControls(); }
  }
  function confirm(title, description, action, successMessage) {
    if(busy || document.querySelector('dialog[open]')) return;
    context.confirmAction(title, description, () => mutate(action), successMessage);
  }
  async function refreshAfter(successMessage) {
    try { if(loading) await loading.catch(() => {}); await refresh(); }
    catch { alert(successMessage + ' 畫面尚未更新，請按「重新讀取帳務」核對最新紀錄。'); }
  }
  function decimal(value, label, positive = false, maximum = '1000000000') {
    const text = String(value).trim();
    if(text.length > 32 || !/^(?:0|[1-9]\d*)(?:\.\d{1,12})?$/.test(text)
      || (positive && !/[1-9]/.test(text))) throw Error(label + '須為有效的非負十進位數字' + (positive ? '，且大於 0。' : '。'));
    const [whole, fraction = ''] = text.split('.');
    if(whole.length > maximum.length || (whole.length === maximum.length &&
      (whole > maximum || (whole === maximum && /[1-9]/.test(fraction))))) throw Error(label + '不得超過 ' + maximum + '。');
    return text;
  }
  function restoreCredit() {
    try {
      const raw = sessionStorage.getItem(pendingCreditKey);
      if(!raw) return;
      const saved = JSON.parse(raw), payload = saved?.payload;
      if(typeof saved?.idempotency_key !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{15,95}$/.test(saved.idempotency_key)
        || !payload || Object.keys(payload).some(key => !['client','amount_usd','note'].includes(key))
        || typeof payload.client !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/.test(payload.client)
        || typeof payload.amount_usd !== 'string' || typeof payload.note !== 'string' || !payload.note.trim() || payload.note.length > 200) throw Error('invalid recovery record');
      decimal(payload.amount_usd,'原入帳金額',true);
      creditIntent = {id:saved.idempotency_key,payload}; recoveredCredit = true; storageIssue = '';
    } catch {
      storageIssue = '無法讀取這個分頁保存的入帳重試紀錄。請先核對帳本，恢復分頁儲存後再操作；不會另換入帳編號。';
    }
  }
  function rememberCredit() {
    try {
      const text = JSON.stringify({idempotency_key:creditIntent.id,payload:creditIntent.payload});
      sessionStorage.setItem(pendingCreditKey,text);
      if(sessionStorage.getItem(pendingCreditKey) !== text) throw Error('recovery record not saved');
      storageIssue = '';
    } catch {
      storageIssue = '瀏覽器無法保存入帳重試編號，尚未送出這次入帳。請先核對帳本並恢復分頁儲存，再沿用原編號重試。';
      throw Error(storageIssue);
    }
  }
  function clearConfirmedCredit() {
    try {
      sessionStorage.removeItem(pendingCreditKey);
      if(sessionStorage.getItem(pendingCreditKey) !== null) throw Error('recovery record not cleared');
      creditIntent = null; recoveredCredit = false; confirmedCredit = false; storageIssue = '';
    } catch {
      storageIssue = '入帳已確認，但瀏覽器尚未清除原重試紀錄。請核對帳本並恢復分頁儲存；再次確認只會沿用同一編號。';
    }
  }
  restoreCredit();
  function clientOptions(selected = '') {
    const clients = data?.clients || [];
    const missing = selected && !clients.some(client => client.id === selected);
    return (missing ? `<option value="${h(selected)}" selected>${h(selected)} · 目前不在客戶清單</option>` : '') + clients.map(client => `<option value="${h(client.id)}" ${client.id === selected ? 'selected' : ''}>${h(client.label || client.id)} · ${h(client.id)}</option>`).join('');
  }
  function markDirty() {
    dirty = true;
    if(q('#billing-settings-form')) settingsDraft = {
      enabled:q('#billing-enabled').checked,multiplier:q('#billing-multiplier').value,
      overrides:Array.from(root.querySelectorAll('[data-billing-override]'),row => [row.querySelector('[data-override-client]').value,row.querySelector('[data-override-rate]').value]),
      prices:Array.from(root.querySelectorAll('[data-billing-price]'),row => [row.querySelector('[data-price-model]').value,{input_per_million:row.querySelector('[data-price-input]').value,output_per_million:row.querySelector('[data-price-output]').value}])
    };
    const indicator = q('#billing-unsaved');
    if(indicator) indicator.hidden = false;
  }
  function overrideRow(client = '', multiplier = '1') {
    const id = ++rowNumber;
    return `<div class="billing-override-row" data-billing-override><label for="billing-override-client-${id}">客戶端<select id="billing-override-client-${id}" data-override-client>${clientOptions(client)}</select></label><label for="billing-override-rate-${id}">此客戶倍率<input id="billing-override-rate-${id}" data-override-rate inputmode="decimal" value="${h(multiplier)}" maxlength="32" required></label><button class="button small" type="button" data-remove-billing-row>移除覆寫</button></div>`;
  }
  function priceRow(model = '', prices = {}) {
    const id = ++rowNumber;
    return `<div class="billing-price-row" data-billing-price><label for="billing-price-model-${id}">精確模型 ID<input id="billing-price-model-${id}" data-price-model value="${h(model)}" maxlength="200" placeholder="供應商 / 模型版本" autocomplete="off" required></label><label for="billing-price-input-${id}">輸入 · USD / 百萬 tokens<input id="billing-price-input-${id}" data-price-input inputmode="decimal" value="${h(prices.input_per_million ?? '')}" maxlength="32" required></label><label for="billing-price-output-${id}">輸出 · USD / 百萬 tokens<input id="billing-price-output-${id}" data-price-output inputmode="decimal" value="${h(prices.output_per_million ?? '')}" maxlength="32" required></label><button class="button small" type="button" data-remove-billing-row>移除售價</button></div>`;
  }
  function bindDraftRows() {
    q('#billing-settings-form').oninput = markDirty;
    q('#billing-settings-form').onchange = markDirty;
    root.querySelectorAll('[data-remove-billing-row]').forEach(button => button.onclick = () => {
      button.closest('[data-billing-override],[data-billing-price]').remove(); markDirty();
    });
  }
  function drawSettings() {
    if(dirty && q('#billing-settings-form')) {
      const changed = settingsRevision !== data.settings.revision;
      q('#billing-settings-version-warning').hidden = !changed;
      q('#billing-save-settings').disabled = changed;
      return;
    }
    const settings = settingsDraft && dirty ? {...settingsDraft,revision:settingsRevision} : data.settings;
    if(!dirty) settingsRevision = settings.revision;
    const overrideRows = dirty && settingsDraft ? settingsDraft.overrides : Object.entries(settings.client_multipliers || {});
    const priceRows = dirty && settingsDraft ? settingsDraft.prices : Object.entries(settings.open_source_prices || {});
    q('#billing-settings').innerHTML = `<form id="billing-settings-form"><div class="billing-settings-top"><label class="billing-checkbox" for="billing-enabled"><input id="billing-enabled" type="checkbox" ${settings.enabled ? 'checked' : ''}>啟用之後新任務的客戶計費</label><span class="badge ${settings.enabled ? 'green' : 'amber'}">${settings.enabled ? '新任務計費已啟用' : '尚未啟用扣款'}</span></div><div class="billing-form-grid"><label for="billing-multiplier">全域折扣／加成倍率<input id="billing-multiplier" inputmode="decimal" value="${h(settings.multiplier)}" maxlength="32" required><small>可填 0–100；1 原價、0.9 九折、1.2 加成 20%。倍率套用至客戶售價。</small></label><div class="billing-policy-note">售價在新任務建立時固定。既有任務沿用原版本；舊任務不補扣，調價不回改已入帳金額。<span id="billing-settings-revision" class="metadata">目前售價版本 ${h(settings.revision)}</span></div></div><div class="billing-section-heading"><div><h3>個別客戶倍率</h3><p>未覆寫的客戶沿用全域倍率；移除後恢復跟隨全域。</p></div><button id="billing-add-override" class="button small" type="button" ${data.clients.length ? '' : 'disabled'}>新增客戶覆寫</button></div><div id="billing-overrides">${overrideRows.map(([client,rate]) => overrideRow(client,rate)).join('')}</div><div class="billing-section-heading"><div><h3>開源模型售價</h3><p>成本估算為 0 仍可設定售價。依精確模型 ID 與實際用量計價，輸入／輸出分開設定。</p></div><button id="billing-add-price" class="button small" type="button">新增模型售價</button></div><div id="billing-prices">${priceRows.map(([model,prices]) => priceRow(model,prices)).join('')}</div><p class="billing-help">每百萬 token 單價上限為 1,000,000 USD。未設定售價、缺少用量或尚未核帳的項目會保留待核，不自動視為免費。</p><div id="billing-settings-version-warning" class="notice billing-warning" role="status" hidden>後端售價版本已變更。請先重新載入設定，再套用你的修改。</div><div class="billing-actions"><span id="billing-unsaved" class="billing-unsaved" hidden>設定尚未保存</span><button id="billing-reload-settings" class="button" type="button">重新載入設定</button><button id="billing-save-settings" class="button primary" type="submit">確認售價設定</button></div></form>`;
    bindDraftRows();
    q('#billing-unsaved').hidden = !dirty;
    q('#billing-settings-version-warning').hidden = settingsRevision === data.settings.revision;
    q('#billing-save-settings').disabled = settingsRevision !== data.settings.revision;
    q('#billing-add-override').onclick = () => { q('#billing-overrides').insertAdjacentHTML('beforeend',overrideRow()); markDirty(); bindDraftRows(); };
    q('#billing-add-price').onclick = () => { q('#billing-prices').insertAdjacentHTML('beforeend',priceRow()); markDirty(); bindDraftRows(); };
    q('#billing-reload-settings').onclick = () => confirm('重新載入帳務設定', '<p>這會放棄本頁尚未保存的售價修改，讀取伺服器目前版本。</p>', async () => { dirty = false; settingsDraft = null; await refresh(); }, '已重新載入帳務設定。');
    q('#billing-settings-form').onsubmit = event => {
      event.preventDefault();
      try {
        const client_multipliers = Object.create(null), open_source_prices = Object.create(null);
        root.querySelectorAll('[data-billing-override]').forEach(row => {
          const client = row.querySelector('[data-override-client]').value;
          if(!client || Object.hasOwn(client_multipliers,client)) throw Error('客戶倍率不可空白或重複。');
          client_multipliers[client] = decimal(row.querySelector('[data-override-rate]').value,'客戶倍率',false,'100');
        });
        root.querySelectorAll('[data-billing-price]').forEach(row => {
          const model = row.querySelector('[data-price-model]').value.trim();
          if(!/^[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,199}$/.test(model) || Object.hasOwn(open_source_prices,model)) throw Error('請填寫有效且不重複的精確模型 ID。');
          open_source_prices[model] = {input_per_million:decimal(row.querySelector('[data-price-input]').value,'輸入單價',false,'1000000'),output_per_million:decimal(row.querySelector('[data-price-output]').value,'輸出單價',false,'1000000')};
        });
        const payload = {revision:settingsRevision,enabled:q('#billing-enabled').checked,multiplier:decimal(q('#billing-multiplier').value,'全域倍率',false,'100'),client_multipliers,open_source_prices};
        confirm('套用新任務的客戶售價', `<p>全域倍率 <strong>${h(payload.multiplier)}</strong>；${Object.keys(client_multipliers).length} 個客戶覆寫、${Object.keys(open_source_prices).length} 個開源模型售價。</p><p>${payload.enabled ? '之後的新任務將綁定此售價版本。' : '之後的新任務不啟用客戶扣款。'}既有任務與已入帳金額保留原版本。</p>`, async () => {
          await context.api('/monitor/billing/settings',payload); dirty = false; settingsDraft = null; await refreshAfter('售價已保存。');
        },'售價已保存，之後的新任務使用新設定。');
      } catch(error) { alert(error.message); }
    };
  }
  function drawAccounts() {
    q('#billing-accounts').innerHTML = `<div class="table-wrap"><table><thead><tr><th>客戶端</th><th>內部美元餘額</th><th>累計入帳</th><th>累計扣款</th></tr></thead><tbody>${data.clients.map(client => `<tr data-billing-client="${h(client.id)}"><td><strong>${h(client.label || client.id)}</strong><br><span class="metadata mono">${h(client.id)}</span></td><td class="amount">${amount(client.balance_usd)}${String(client.balance_usd).startsWith('-') ? '<br><span class="badge amber">待收款</span>' : ''}</td><td class="amount">${amount(client.credits_usd)}</td><td class="amount">${amount(client.charges_usd)}</td></tr>`).join('') || '<tr><td colspan="4" class="empty">尚無客戶端</td></tr>'}</tbody></table></div><div class="table-note">這是客戶內部帳本，與 OpenRouter 帳戶餘額、核心派送預算分開。負餘額表示待收款。</div>`;
  }
  function drawCredit() {
    const values = creditIntent?.payload;
    const previous = q('#billing-credit-form');
    const retained = values || (previous ? {client:q('#billing-credit-client').value,amount_usd:q('#billing-credit-amount').value,note:q('#billing-credit-note').value} : {});
    const locked = !!values;
    const missingClient = !!retained.client && !data.clients.some(client => client.id === retained.client);
    q('#billing-credit').innerHTML = `<form id="billing-credit-form" class="billing-credit-form"><label for="billing-credit-client">客戶端<select id="billing-credit-client" ${locked ? 'disabled' : ''} required>${missingClient ? `<option value="${h(retained.client)}">${h(retained.client)} · 原客戶目前不在清單</option>` : clientOptions(retained.client)}</select></label><label for="billing-credit-amount">入帳金額 · USD<input id="billing-credit-amount" inputmode="decimal" value="${h(retained.amount_usd || '')}" maxlength="32" placeholder="例如 10.00" ${locked ? 'disabled' : ''} required></label><label for="billing-credit-note">入帳備註<input id="billing-credit-note" value="${h(retained.note || '')}" maxlength="200" placeholder="必填：對帳參考或原因" ${locked ? 'disabled' : ''} required></label><button id="billing-credit-submit" class="button primary" type="submit" ${data.clients.length && !missingClient && !storageIssue ? '' : 'disabled'}>${locked ? '重試同一筆入帳' : '確認客戶入帳'}</button></form>${locked ? `<p id="billing-credit-recovery" class="billing-help billing-warning" role="status">${confirmedCredit ? '上一筆入帳已由伺服器確認，瀏覽器重試紀錄仍待清除。' : recoveredCredit ? '已找回重新載入前尚待確認的入帳紀錄。' : '上一筆入帳尚未確認。'}重試會沿用原內容與同一編號，避免重複入帳；確認成功前不另建新單。</p>` : '<p class="billing-help">只記錄已確認的客戶入帳，不會刷卡、轉帳或向 OpenRouter 購買點數。</p>'}${missingClient ? '<p class="billing-help billing-warning" role="alert">原客戶目前不在清單，已停止重試。請核對帳本與客戶身分；不會將原入帳套用到其他客戶。</p>' : ''}${storageIssue ? `<p id="billing-credit-storage-warning" class="billing-help billing-warning" role="alert">${h(storageIssue)}</p>` : ''}`;
    q('#billing-credit-form').onsubmit = event => {
      event.preventDefault();
      try {
        const payload = creditIntent?.payload || {client:q('#billing-credit-client').value,amount_usd:decimal(q('#billing-credit-amount').value,'入帳金額',true),note:q('#billing-credit-note').value.trim()};
        if(!payload.client || !data.clients.some(client => client.id === payload.client)) throw Error('請先核對並選擇目前存在的客戶端。');
        if(!payload.note.trim()) throw Error('請填寫入帳備註，保留可核對的原因或參考。');
        confirm(creditIntent ? '重試原客戶入帳' : '記錄客戶美元入帳',`<p>客戶端 <strong>${h(payload.client)}</strong>，入帳 <strong>${amount(payload.amount_usd)} USD</strong>。</p><p>${h(payload.note || '未填備註')}</p><p>本次僅新增內部帳本紀錄，不會執行金流轉帳。</p>`,async () => {
          if(!creditIntent) creditIntent = {id:crypto.randomUUID(),payload};
          try { rememberCredit(); await context.api('/monitor/billing/credits',{...creditIntent.payload,idempotency_key:creditIntent.id}); }
          catch(error) { if(q('#billing-credit')) { drawCredit(); busyControls(); } throw error; }
          confirmedCredit = true; clearConfirmedCredit();
          if(!creditIntent && q('#billing-credit-amount')) q('#billing-credit-amount').value = '';
          if(!creditIntent && q('#billing-credit-note')) q('#billing-credit-note').value = '';
          if(q('#billing-credit')) { drawCredit(); busyControls(); }
          await refreshAfter('客戶入帳已成功記錄。');
        },'客戶美元入帳已記錄。');
      } catch(error) { alert(error.message); }
    };
  }
  function drawSource() {
    const source = data.source || {}, account = source.account || {}, key = account.key || {}, total = account.account || {};
    const period = key.limit_reset === 'daily' ? {reset:'每日',current:'本日',usage:key.usage_daily_usd}
      : key.limit_reset === 'weekly' ? {reset:'每週',current:'本週',usage:key.usage_weekly_usd}
      : key.limit_reset === 'monthly' ? {reset:'每月',current:'本月',usage:key.usage_monthly_usd} : null;
    const limitPeriod = period?.current || '週期未知';
    q('#billing-source').innerHTML = `<div class="billing-source-heading"><div><h3>OpenRouter 實際帳務資料</h3><p id="billing-source-status">${source.configured ? h(statusLabels[account.status] || '已設定，等待手動讀取') : '尚未設定帳務來源，沒有發送帳務 API 請求。'}</p></div><button id="billing-refresh-source" class="button" type="button" ${source.configured ? '' : 'disabled'}>更新 OpenRouter 帳務資料</button></div><div class="billing-source-grid"><section aria-labelledby="billing-key-heading"><h3 id="billing-key-heading">此 API 金鑰範圍</h3><dl><div><dt>累計用量（所有期間）</dt><dd>${amount(key.usage_usd)}</dd></div><div><dt>費用上限重設週期</dt><dd>${period?.reset || '未知'}</dd></div>${period ? `<div><dt>${period.current}用量</dt><dd>${amount(period.usage)}</dd></div>` : ''}<div><dt>金鑰費用上限（${limitPeriod}）</dt><dd>${amount(key.limit_usd)}</dd></div><div><dt>上限剩餘（${limitPeriod}）</dt><dd>${amount(key.limit_remaining_usd)}</dd></div><div><dt>BYOK 用量</dt><dd>${amount(key.byok_usage_usd)}</dd></div></dl><p class="metadata">${h(statusLabels[key.status] || '尚未取得')}${key.error ? ' · ' + h(errorText(key.error)) : ''}</p></section><section aria-labelledby="billing-account-heading"><h3 id="billing-account-heading">OpenRouter 帳戶範圍</h3><dl><div><dt>累計購買 credits</dt><dd>${amount(total.total_credits_usd)}</dd></div><div><dt>累計帳戶用量</dt><dd>${amount(total.total_usage_usd)}</dd></div></dl><p class="metadata">${h(statusLabels[total.status] || '尚未取得')}${total.error ? ' · ' + h(errorText(total.error)) : ''}。帳戶資料需有權限的管理金鑰。</p></section></div><p class="billing-help">金鑰、OpenRouter 帳戶與客戶餘額是不同範圍，不以帳戶總用量分攤客戶費用。客戶扣款依每次請求的實際核帳資料或已設定的開源模型 token 售價計算。OpenRouter 費用不包含外部 BYOK 供應商另行收取的費用。</p><p class="metadata">最近讀取：<span id="billing-source-checked">${h(stamp(source.last_checked || account.observed_at))}</span>${source.error ? ' · ' + h(errorText(source.error)) : ''}</p>`;
    q('#billing-refresh-source').onclick = () => confirm('讀取 OpenRouter 帳務資料','<p>會以伺服器配置的憑證唯讀查詢 OpenRouter 帳務資料，不會派送模型、入帳或扣款。</p>',async () => {
      await context.api('/monitor/billing/source/account',{}); await refreshAfter('OpenRouter 帳務查詢已完成。');
    },'OpenRouter 帳務資料已更新；未執行客戶扣款。');
  }
  function pricingDetails(entry) {
    if(entry.kind !== 'debit') return '';
    return `<details class="billing-pricing-details" data-billing-pricing-detail><summary>計價明細</summary><dl><div><dt>供應商核帳成本 · USD</dt><dd>${amount(entry.supplier_cost_usd)}</dd></div><div><dt>實際使用倍率</dt><dd>${decimalText(entry.multiplier)}</dd></div><div><dt>輸入 tokens</dt><dd>${tokenCount(entry.input_tokens)}</dd></div><div><dt>輸出 tokens</dt><dd>${tokenCount(entry.output_tokens)}</dd></div>${entry.token_price == null ? '' : `<div><dt>輸入售價 · USD / 百萬 tokens</dt><dd>${amount(entry.token_price.input_per_million)}</dd></div><div><dt>輸出售價 · USD / 百萬 tokens</dt><dd>${amount(entry.token_price.output_per_million)}</dd></div>`}</dl></details>`;
  }
  function drawEntries() {
    q('#billing-entries').innerHTML = `<div class="table-wrap"><table><thead><tr><th>時間／編號</th><th>客戶端／任務</th><th>類型</th><th>金額 · USD</th><th>計價依據／模型</th></tr></thead><tbody>${data.entries.map(entry => `<tr data-billing-entry="${h(entry.id)}"><td>${h(stamp(entry.created))}<br><span class="metadata mono">${h(entry.id)}</span></td><td>${h(entry.client)}<br><span class="metadata mono">${h(entry.run_id || '—')}</span></td><td><span class="badge ${entry.kind === 'credit' ? 'green' : 'blue'}">${entry.kind === 'credit' ? '入帳' : '扣款'}</span></td><td class="amount">${entry.kind === 'credit' ? '+' : '−'}${amount(entry.amount_usd)}</td><td>${h(basisLabels[entry.basis] || (entry.kind === 'credit' ? '客戶入帳' : entry.basis || '依固定售價'))}<br><span class="metadata mono">${h(entry.model || '—')}</span>${entry.pricing_revision == null ? '' : `<br><span class="metadata">售價版本 ${h(entry.pricing_revision)}</span>`}${pricingDetails(entry)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">尚無入帳或扣款紀錄</td></tr>'}</tbody></table></div>`;
    q('#billing-pending').innerHTML = `<div class="table-wrap"><table><thead><tr><th>客戶端／任務</th><th>模型</th><th>待核原因</th></tr></thead><tbody>${data.pending.map(item => `<tr><td>${h(item.client || '歸屬待核')}<br><span class="metadata mono">${h(item.run_id || '—')}</span></td><td class="mono">${h(item.model || '模型待核')}</td><td>${h(errorText(item.reason))}</td></tr>`).join('') || '<tr><td colspan="3" class="empty">目前沒有待核項目</td></tr>'}</tbody></table></div>`;
    q('#billing-pending-count').textContent = String(data.pending.length);
    q('#billing-reconcile').disabled = false;
    q('#billing-reconcile').onclick = () => confirm('核帳並記錄客戶扣款',`<p>依任務建立時固定的售價核帳。已扣款的請求沿用原紀錄；未核實的費用或缺少用量的項目仍待核。</p><p>${data.demo ? '本環境使用合成任務與費用，產生的是合成帳務紀錄。' : '需要時會唯讀查詢 OpenRouter 的實際請求帳務，不會重送模型請求。'}本次不執行外部金流。</p>`,async () => {
      const result = await context.api('/monitor/billing/reconcile',{});
      if(q('#billing-reconcile-result')) q('#billing-reconcile-result').textContent = `本次記錄 ${result.charged ?? 0} 筆扣款，${result.waiting ?? 0} 筆仍待核。`;
      await refreshAfter('本次核帳操作已完成。');
    },'核帳已完成；已確認的扣款與待核項目分別保留。');
  }
  async function refresh() {
    if(!root || !context || !root.querySelector('#billing-console')) return;
    if(loading) return loading;
    const mounted = root, version = mountVersion, request = ++requestVersion;
    loading = (async () => {
      try {
        const result = await context.api('/monitor/billing');
        if(root !== mounted || version !== mountVersion || !root.querySelector('#billing-console')) return;
        data = {...result,clients:result.clients || [],entries:result.entries || [],pending:result.pending || []};
        alert('');
        q('#billing-environment').textContent = data.demo ? '合成帳務 · 非實際扣款' : '內部美元帳本';
        q('#billing-environment').className = 'badge ' + (data.demo ? 'amber' : 'blue');
        q('#billing-notice').textContent = data.notice || '供應商實際成本、成本估算與客戶售價分別記錄。';
        drawSettings(); drawAccounts(); drawCredit(); drawSource(); drawEntries(); busyControls();
        q('#billing-loaded').textContent = '讀取 ' + new Date().toLocaleTimeString('zh-TW',{hour12:false});
      } catch(error) {
        if(root === mounted && version === mountVersion) alert('帳務資料未更新：' + errorText(error.message || '請稍後重新讀取。'));
        throw error;
      } finally { if(request === requestVersion) loading = null; }
    })();
    return loading;
  }
  function mount(mainElement, helpers) {
    root = mainElement; context = helpers;
    if(root.querySelector('#billing-console')) return;
    ++mountVersion; loading = null; data = null;
    root.innerHTML = `<div id="billing-console"><div class="page-heading"><div><div class="eyebrow">CUSTOMER BILLING / USD</div><h1>客戶帳務與售價</h1><p>依實際帳務核對成本，設定客戶售價與內部美元扣款。</p></div><div class="billing-heading-actions"><span id="billing-environment" class="badge amber">讀取帳務中</span><button id="billing-reload" class="button" type="button">重新讀取帳務</button><span id="billing-loaded" class="metadata"></span></div></div><div id="billing-alert" class="notice billing-warning" role="alert" hidden></div><div id="billing-notice" class="notice">供應商實際成本、成本估算與客戶售價分別記錄。</div><p id="billing-busy" class="billing-help" role="status" hidden>帳務操作進行中，請等候原結果。</p><section class="panel" aria-labelledby="billing-source-title"><div class="panel-header"><h2 id="billing-source-title">供應商帳務</h2><span class="badge">唯讀查詢</span></div><div id="billing-source" class="panel-body"><p>正在讀取來源狀態…</p></div></section><section class="panel mt" aria-labelledby="billing-settings-title"><div class="panel-header"><h2 id="billing-settings-title">客戶計價參數</h2><span class="badge">新任務固定版本</span></div><div id="billing-settings" class="panel-body"><p>正在讀取售價…</p></div></section><section class="panel mt" aria-labelledby="billing-accounts-title"><div class="panel-header"><h2 id="billing-accounts-title">客戶美元帳本</h2><span class="badge">內部紀錄</span></div><div id="billing-accounts"></div><div class="panel-body billing-credit-panel"><h3>記錄客戶入帳</h3><div id="billing-credit"><p>正在讀取客戶…</p></div></div></section><section class="panel mt" aria-labelledby="billing-postings-title"><div class="panel-header"><div><h2 id="billing-postings-title">入帳與扣款紀錄</h2><p id="billing-reconcile-result" role="status"></p></div><button id="billing-reconcile" class="button primary" type="button" disabled>核帳並扣款</button></div><div id="billing-entries"></div></section><section class="panel mt" aria-labelledby="billing-pending-title"><div class="panel-header"><h2 id="billing-pending-title">待核項目</h2><span id="billing-pending-count" class="badge amber">—</span></div><div id="billing-pending"></div></section></div>`;
    q('#billing-reload').onclick = () => refresh().catch(() => {});
    refresh().then(() => { if(q('#billing-reconcile')) q('#billing-reconcile').disabled = false; }).catch(() => {});
  }
  function resetView() {
    ++mountVersion; ++requestVersion; loading = null; root = null; context = null; data = null;
    dirty = false; settingsRevision = null; settingsDraft = null;
    // A logout never discards an unconfirmed financial identity.
  }
  return {mount,refresh,resetView};
})();
