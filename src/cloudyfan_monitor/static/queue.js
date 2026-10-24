'use strict';
window.CloudyFanQueue = (() => {
  let root, context, data = null, flight = null, generation = 0, requestVersion = 0, readState = 'idle';
  const h = value => context.esc(value);
  const states = {
    accepting: ['正在保存入列', 'blue'], queued: ['排隊中', 'blue'], claiming: ['正在核對名額', 'blue'],
    running: ['執行中', 'blue'], interrupted_pending: ['中斷待核對', 'amber'],
    completed: ['已結束', ''], rejected: ['已拒絕', 'amber'], cancelled: ['已取消等待', ''], expired: ['已到期', 'amber'],
    passed: ['通過', 'green'], failed: ['未通過', 'red'], unverifiable: ['無法確認', 'amber'],
    capacity_denied: ['容量條件不符', 'amber'], policy_denied: ['執行條件不符', 'amber'],
    engineering_interrupted: ['工程中斷', 'amber'], accounting_interrupted: ['費用待核', 'amber'],
  };
  const reasons = {
    queue_waiting_worker: '工作正在等待受控派送程序領取下一步；等待本身不新增模型請求，已有請求與費用仍保留。',
    queue_waiting_slot: '正在等待執行名額；尚未取得執行名額。',
    queue_waiting_window: '正在等待適用的供應商窗口；等待不占用執行名額。',
    queue_full: '已達固定佇列容量，未建立另一筆可派送工作。',
    queue_original_authority_denied: '原任務的授權或固定執行條件未通過；停止新的派送，保留原任務與證據。',
    principal_quota_full: '此客戶端已達固定待處理上限。',
    queue_deadline_expired: '已達原等待期限，不再派送；原任務身分不重新建立。',
    cancelled_before_dispatch: '已在模型派送前取消；重送原識別只讀取這個結果。',
    cancelled_before_next_dispatch: '已取消下一步等待；保留先前送出的請求、原回應及費用，不表示退款或可重新開始。',
    worker_interrupted: '工作程序中斷，保留未決狀態與原證據，不自動重送或釋放名額。',
    execution_interrupted: '執行中斷，須核對原證據，不自動重送。',
    queue_clock_invalid: '時間無法可信判讀，不能以目前時間推定可派送或到期。',
    queue_worker_protocol_unavailable: '受控派送協定尚無法使用，未派送新的請求。',
    capacity_window_wait: '正在等待適用的供應商窗口；尚未取得執行名額。',
    capacity_concurrency_exhausted: '適用群組的並行名額已滿，尚未取得新的執行名額。',
    capacity_rpm_exhausted: '原窗口的保守請求計數已達上限，等待窗口可准入。',
    capacity_tpm_exhausted: '原窗口的保守 token 預留已達上限，等待窗口可准入。',
    capacity_deadline_exceeded: '已達原任務期限，不再派送。',
    capacity_credential_inactive: '原任務的授權或憑證已失效，不再派送。',
    capacity_credential_unavailable: '原任務憑證無法核對，不再派送。',
    task_expired: '已達原任務存取期限，原結果與接續不再開放。',
  };
  const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const shown = value => finite(value) ? String(value) : '未知';
  const stamp = value => finite(value) ? new Date(value * 1000).toLocaleString('zh-TW', {hour12: false}) : '未知';
  const seconds = (value, missing = '未知') => finite(value) ? value.toFixed(2) + ' 秒' : missing;
  const money = value => typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value) ? '$' + value : '未知';

  function rejection(item) {
    const known = typeof item.reason === 'string' && Object.hasOwn(reasons, item.reason);
    return `<article class="queue-rejection" data-queue-rejection="${h(item.run_id)}"><div><strong class="mono">${h(item.run_id)}</strong><p>客戶端 <span class="mono">${h(item.client)}</span> · 原拒絕時間 ${h(stamp(item.at))}</p></div><p class="queue-reason">${h(known ? reasons[item.reason] : '目前沒有可核對的安全原因，請由操作員查核原保存證據。')}${known ? `<small class="mono">${h(item.reason)}</small>` : ''}</p></article>`;
  }

  function binding(queue) {
    const frozen = queue.frozen || {}, monitor = frozen.monitor || {}, capacity = frozen.capacity || {}, tariff = monitor.tariff || {};
    const text = value => typeof value === 'string' && value ? value : '未知';
    const field = (label, key, value) => `<div><dt>${h(label)}</dt><dd data-queue-frozen="${key}">${h(value)}</dd></div>`;
    return `<h3 class="queue-binding-heading">入列時保存的執行設定</h3><p class="metadata">以下沿用這筆任務的原設定；不以目前控制台的模式、費率或版本替代。</p><dl class="queue-properties queue-binding">${[
      field('原執行契約', 'contract', text(frozen.contract_id)),
      field('原契約版本', 'contract-version', text(frozen.contract_version)),
      field('原契約核對值', 'registered-sha', text(frozen.registered_sha256)),
      field('原路由模式', 'mode', text(monitor.mode || frozen.mode)),
      field('原候選池版本', 'pool', text(monitor.pool_version)),
      field('原任務分類', 'category', text(monitor.category)),
      field('原費率修訂', 'tariff-revision', shown(tariff.revision)),
      field('原客戶端費率倍率', 'tariff-multiplier', text(tariff.multiplier)),
      field('原費率收費設定', 'tariff-enabled', tariff.enabled === true ? '已啟用' : tariff.enabled === false ? '未啟用' : '未知'),
      field('原費率與自訂報價核對值', 'tariff-sha', text(monitor.tariff_sha256)),
      field('原容量政策版本', 'capacity-policy', text(capacity.policy_version)),
      field('原容量設定版本', 'capacity-configuration', text(capacity.configuration_version)),
      field('原容量設定核對值', 'configuration-sha', text(capacity.configuration_sha256)),
      field('原排隊版本', 'queue-version', text(capacity.queue_version)),
      field('原窗口版本', 'windows-version', text(capacity.windows_version)),
      field('原任務存取期限', 'access-expiry', stamp(frozen.access_expires_at)),
    ].join('')}</dl>`;
  }

  function job(item) {
    const accessExpired = item.reason === 'task_expired' || item.observation_error === 'task_expired';
    const state = accessExpired ? ['存取已到期', 'amber'] :
      item.observation_error ? ['原證據無法核對', 'amber'] :
      item.status === 'cancelled' && item.reason === 'cancelled_before_dispatch' ? ['派送前已取消', ''] :
      item.status === 'cancelled' && item.reason === 'cancelled_before_next_dispatch' ? ['已取消後續等待', ''] :
      Object.hasOwn(states, item.status) ? states[item.status] : ['狀態無法確認', 'amber'];
    const queue = item.queue || {}, costs = item.costs || {};
    const codes = [...new Set((accessExpired ? ['task_expired'] : [item.reason, item.capacity_reason, item.observation_error]).filter(code => typeof code === 'string' && code))];
    const knownCodes = codes.filter(code => Object.hasOwn(reasons, code));
    const reasonRows = knownCodes.map(code => `<p class="queue-reason">${h(reasons[code])}<small class="mono">${h(code)}</small></p>`).join('') +
      (knownCodes.length < codes.length ? '<p class="queue-reason">目前沒有可核對的安全原因，請由操作員查核原保存證據。</p>' : '');
    return `<article class="panel queue-job" data-queue-run="${h(item.run_id)}"><div class="queue-job-heading"><div><h2 class="mono">${h(item.run_id)}</h2><p>客戶端 <span class="mono">${h(item.client)}</span></p></div><span class="badge ${state[1]}">${h(state[0])}</span></div>${reasonRows}${item.cancellation_requested ? `<p class="queue-warning">${item.queue_state === 'cancelled' ? item.reason === 'cancelled_before_dispatch' ? '派送前取消已記錄。' : '後續等待已取消，既有請求、費用與未決狀態仍保留。' : '已提出取消要求；未確認供應商停止，不代表退款或可安全重送。'}</p>` : ''}<dl class="queue-timings"><div><dt>入列至首次執行</dt><dd data-queue-timing="queue">${h(seconds(item.queue_seconds, item.queue_state === 'queued' ? '尚未開始' : '未知'))}</dd></div><div><dt>累計等待時間</dt><dd data-queue-timing="waiting">${h(seconds(queue.waiting_seconds))}</dd></div><div><dt>完整流程耗時</dt><dd data-queue-timing="end_to_end">${h(seconds(item.end_to_end_seconds, ['queued', 'accepting', 'claiming', 'running', 'interrupted_pending'].includes(item.queue_state) ? '尚未完成' : '未知'))}</dd></div></dl><details data-queue-detail="${h(JSON.stringify([item.client, item.run_id]))}"><summary>查看原期限、版本與費用</summary><dl class="queue-properties"><div><dt>原入列時間</dt><dd>${h(stamp(queue.accepted_at))}</dd></div><div><dt>原等待期限</dt><dd>${h(stamp(queue.deadline))}</dd></div><div><dt>固定排隊版本</dt><dd class="mono">${h(queue.version || '未知')}</dd></div><div><dt>完整 API 費用</dt><dd>${h(money(costs.complete_api_usd))}</dd></div><div><dt>已知 API 費用</dt><dd>${h(money(costs.known_api_usd))}</dd></div><div><dt>費用未明事件</dt><dd>${h(shown(costs.unknown_events))}</dd></div></dl>${binding(queue)}${queue.effective_expiry_only ? '<p class="metadata">目前依原期限顯示到期；此唯讀觀測不代表派送程序已寫入新的狀態。</p>' : ''}</details></article>`;
  }

  function render() {
    if (!root?.querySelector('#queue-page')) return;
    const opened = new Set(Array.from(root.querySelectorAll('details[open]')).map(node => node.dataset.queueDetail));
    const active = document.activeElement;
    const focusedDetail = root.contains(active) && active?.tagName === 'SUMMARY' ? active.closest('details')?.dataset.queueDetail : null;
    const issue = readState === 'unauthorized' ? '<p class="queue-warning" role="status">操作員授權已失效，已清除這個排隊畫面；請重新認證。</p>' : readState === 'stale' ? '<p class="queue-warning" role="status">本次讀取失敗，保留上次畫面與原始觀測時間。</p>' : readState === 'unavailable' ? '<p class="queue-warning" role="status">排隊資料目前無法讀取；本頁沒有派送或重新入列。</p>' : '';
    const limits = data?.limits || {}, jobs = Array.isArray(data?.jobs) ? data.jobs : [], refused = Array.isArray(data?.rejections) ? data.rejections : [];
    root.innerHTML = `<div id="queue-page"><div class="page-heading"><div><div class="eyebrow">BOUNDED TASK QUEUE</div><h1>排隊與任務生命週期</h1><p>分清等待名額、等待窗口與執行狀態；查詢不派送、不刷新供應商來源。</p></div></div><p class="notice queue-client-notice" role="note">客戶端需支援 202 排隊狀態與後續查詢；只接受立即答案的客戶端需先調整。</p>${context.demo ? '<p class="queue-warning">合成驗收環境 · 耗時只描述本次合成流程，未建立真實供應商吞吐或 SLO 資格。</p>' : ''}${issue}${!data ? `<div class="panel empty">${readState === 'unauthorized' ? '重新認證後才能讀取排隊工作。' : '正在讀取已保存的排隊資料…'}</div>` : !data.configured ? '<div class="panel empty">尚未接入版本化有界佇列；保留原同步入口規則。</div>' : `<section class="panel queue-configuration"><h2>固定的等待範圍</h2><p>排隊版本 <code>${h(data.version)}</code> · 原觀測時間 <span id="queue-observed-at">${h(stamp(data.now))}</span></p><dl class="queue-limits"><div><dt>佇列容量</dt><dd data-queue-limit="capacity">${h(shown(limits.capacity))}</dd></div><div><dt>執行並行數</dt><dd data-queue-limit="concurrency">${h(shown(limits.concurrency))}</dd></div><div><dt>每客戶端待處理上限</dt><dd data-queue-limit="per_principal">${h(shown(limits.per_principal))}</dd></div><div><dt>最長等待</dt><dd data-queue-limit="max_wait_seconds">${h(shown(limits.max_wait_seconds))}${finite(limits.max_wait_seconds) ? ' 秒' : ''}</dd></div></dl><p class="metadata">以上為伺服器固定設定；本頁只讀。取消由原客戶端提出，執行中取消僅記錄要求，不承諾供應商停止或退款。排隊與接續不延長原任務存取期限。</p></section><p class="metadata queue-active-count">目前占用待處理名額 ${h(shown(data.active_count))} 件；保存中的待處理紀錄 ${h(shown(data.retained_active_count))} 件${data.active_history_complete === false ? '，本頁未列出全部待處理工作。' : '。'}</p><p class="metadata queue-count">保存 ${h(shown(data.job_count))} 件工作；本頁列出 ${jobs.length} 件${data.history_complete === false ? '，僅為部分歷史，不能由此推定全部占用。' : '。'}</p><section class="panel queue-rejections"><h2>未入列的拒絕紀錄</h2><p class="metadata queue-rejection-count">保存 ${h(shown(data.rejection_count))} 件拒絕紀錄；本頁列出 ${refused.length} 件${data.rejection_history_complete === false ? '，僅為部分歷史。' : '。'}</p>${refused.map(rejection).join('') || '<p class="metadata">目前沒有可讀取的拒絕紀錄。</p>'}</section><div class="queue-jobs">${jobs.map(job).join('') || '<div class="panel empty">尚無可讀取的排隊工作。</div>'}</div>`}</div>`;
    root.querySelectorAll('details').forEach(node => { node.open = opened.has(node.dataset.queueDetail); });
    if (focusedDetail) {
      const target = Array.from(root.querySelectorAll('details')).find(node => node.dataset.queueDetail === focusedDetail)?.querySelector('summary');
      const fallback = target || document.getElementById('reload') || root.querySelector('#queue-page');
      if (fallback.id === 'queue-page') fallback.tabIndex = -1;
      fallback.focus({preventScroll: !!target});
    }
  }

  function refresh(force = false) {
    if (!context || !root?.querySelector('#queue-page')) return Promise.resolve();
    if (readState === 'unauthorized' && !force) return Promise.resolve();
    if (flight && !force) return flight;
    const currentGeneration = generation, currentRequest = ++requestVersion;
    const pending = (async () => {
      try {
        const next = await context.api('/monitor/queue');
        if (generation !== currentGeneration || currentRequest !== requestVersion || !root?.querySelector('#queue-page')) return;
        data = next; readState = 'current'; render();
      } catch (error) {
        if (generation !== currentGeneration || currentRequest !== requestVersion || !root?.querySelector('#queue-page')) return;
        if (error.status === 401) { data = null; readState = 'unauthorized'; }
        else readState = data ? 'stale' : 'unavailable';
        render();
      } finally { if (flight === pending) flight = null; }
    })();
    flight = pending;
    return pending;
  }

  function mount(node, nextContext) {
    const sameSession = context?.sessionVersion === nextContext.sessionVersion;
    context = nextContext;
    if (sameSession && root === node && root.querySelector('#queue-page')) return;
    generation += 1; root = node; data = null; flight = null; readState = 'idle';
    root.innerHTML = '<div id="queue-page"></div>';
    render(); refresh();
  }
  function resetView() { generation += 1; requestVersion += 1; root = null; context = null; data = null; flight = null; readState = 'idle'; }
  setInterval(() => refresh(), 5000);
  return {mount, refresh, resetView};
})();
