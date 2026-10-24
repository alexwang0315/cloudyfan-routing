'use strict';
window.CloudyFanCapacity = (() => {
  let root = null, context = null, data = null, readState = 'idle', generation = 0, flight = null;
  let requestVersion = 0, policySelection = null, mutationBusy = false, activationError = '';
  let historyTasks = null, historyState = 'idle';
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
    capacity_revision_conflict: '容量設定已由另一個操作更新；請重新核對版本後再明示套用。',
    capacity_policy_not_registered: '這個容量政策沒有在伺服器登錄，不能啟用。',
    capacity_live_state_blocks_change: '仍有執行中任務、未決名額或有效窗口用量，暫時不能切換或停用。',
    capacity_dispatch_not_configured: '尚未接入受控容量派送，保留原服務規則。',
    capacity_rpm_exhausted: '原窗口的請求數已達上限，這次請求在送出前遭拒絕。',
    capacity_tpm_exhausted: '原窗口的 token 預留已達上限，這次請求在送出前遭拒絕。',
    capacity_concurrency_exhausted: '適用群組的並行名額已滿，沒有增加另一筆派送。',
    capacity_token_bound_unavailable: '缺少適用實際請求的可信 token 上界，不能通過 TPM 准入。',
    capacity_token_bound_invalid: 'token 上界與實際請求或設定無法對應，停止派送。',
    capacity_token_bound_expired: 'token 上界證據已過期，停止新的派送。',
    capacity_token_bound_violated: '已回報的 token 用量超過原上界，停止後續派送並保留原證據。',
    capacity_supplier_evidence_unavailable: '這個政策要求的新鮮供應商限制尚無法確認，停止派送。',
    capacity_local_limit_unavailable: '缺少適用的本機保守限制或不適用依據，停止派送。',
    capacity_evidence_changed_or_expired: '適用的容量證據已變更或過期，停止新的派送。',
    capacity_clock_backwards: '時間倒退，不能可信判定容量窗口，停止新的派送。',
    capacity_deadline_exceeded: '已達任務原期限，不再派送。',
    capacity_credential_inactive: '原派送身分或憑證已失效，不再派送。',
    capacity_credential_unavailable: '原派送憑證無法確認，不再派送。',
    capacity_route_not_registered: '這次請求不在原政策的已登錄派送範圍，停止派送。',
    capacity_frozen_adoption_changed: '保存的任務容量版本或設定無法核對，停止派送。',
    capacity_transport_protocol_unsupported: '這個 transport 尚無適用容量檢查協定，不能依新政策派送。',
    capacity_window_wait: '原記錄顯示適用共用群組處於限流窗口；新派送仍須另經原資格、健康與費用閘門。',
    capacity_window_unknown: '窗口狀態無法確認，保留限制並停止適用的新派送。',
    capacity_window_header_invalid: '原回應的限流時間無法可信換算，不能當作零或自動清除暫停。',
    capacity_window_header_missing: '原限流回應缺少政策要求的時間欄位，不能推定窗口為零。',
    capacity_window_clock_reference_unknown: '原回應的時間參考無法確認，保留未知窗口，不以現在時間補造重置時間。',
    capacity_window_time_invalid: '原回應的窗口時間不合法或超出可信範圍，不解除暫停。',
    capacity_window_scope_or_clock_invalid: '窗口範圍或時鐘無法確認，不放行新的派送。',
    capacity_window_scope_unsupported: '這個適用範圍尚無明示窗口規則，不推定為可用。',
    capacity_window_backend_unavailable: '已登錄的窗口檢查目前不可用，不能略過限制。',
    capacity_window_state_unavailable: '保存的窗口資料無法確認，停止適用的新派送。',
    capacity_window_observation_unavailable: '原回應的窗口證據保存未確認，保留原收據與保守占用。',
    capacity_window_source_contradictory: '保存來源的窗口時間互相矛盾，不清除原暫停。',
    capacity_window_clock_backwards: '時鐘倒退，不能用目前時間清除原窗口。',
    capacity_window_source_not_imported: '這筆受控來源尚未明示匯入；只影響原適用範圍的候選，查詢不解除這項限制。',
    capacity_window_source_state_unavailable: '受控來源的匯入狀態無法確認，適用候選保持未知，不自動清除來源。',
    capacity_window_open: '這次保存的檢查未排除候選；仍須通過其他原閘門。',
    capacity_window_not_applicable: '原政策明示未採用這項窗口檢查。',
  };
  const reason = code => reasons[code] || '來源資料無法確認，保留安全原因供查核。';
  const stamp = value => typeof value !== 'number' || !Number.isFinite(value) || value < 0 || !Number.isFinite(new Date(value * 1000).getTime()) ? '未知' : new Date(value * 1000).toLocaleString('zh-TW', { hour12: false });
  const period = value => ({ daily: '每日', weekly: '每週', monthly: '每月' }[value] || '未知');
  const shown = value => value === null || value === undefined ? '未知' : String(value);

  function windowCard(row, historical = false) {
    if (!row || typeof row !== 'object') return '<p class="capacity-warning">一筆原窗口資料無法確認，不據此解除適用限制。</p>';
    const state = row.status === 'wait' ? '原窗口要求等待' : '未知，停止適用派送';
    const source = row.source_kind === 'original_response' ? '已保存原回應' : row.source_kind === 'controlled_import' ? '已明示匯入的受控保存來源' : '來源尚未確認';
    return `<details class="capacity-window" data-capacity-window="${h(row.id)}" data-capacity-detail="${h((historical ? 'window-history:' : 'window:') + row.id)}"><summary><span><strong>${h(row.group || '群組未知')}</strong><small>${h(scopes[row.scope?.kind] || '範圍未知')} · ${h(row.scope?.ref || '未知')}</small></span><span class="badge amber">${h(state)}</span></summary><div class="capacity-window-body">${row.observation_status === 'unavailable' ? '<p class="capacity-warning">本次窗口保存狀態無法核對；以下保留原保存時間與事件，不能由舊資料解除適用限制。</p>' : ''}<dl><div><dt>原觀測時間</dt><dd data-window-time="observed">${h(stamp(row.observed_at))}</dd></div><div><dt>原限制最早可再評估時間</dt><dd data-window-time="not-before">${h(stamp(row.not_before))}</dd></div>${Object.hasOwn(row, 'reported_not_before') ? `<div><dt>來源原報告的窗口時間</dt><dd>${h(stamp(row.reported_not_before))}<br><small>原報告值不是目前放行時間；來源矛盾時仍保留未知限制。</small></dd></div>` : ''}<div><dt>可信來源 / 時間欄位</dt><dd>${h(source)} · <code>${h(row.header || '未知')}</code>${row.source_kind === 'controlled_import' ? `<br>來源版本 <code>${h(row.source_version || '未知')}</code><br>原證據 <code>${h(row.evidence_ref || '未知')}</code>` : ''}</dd></div><div><dt>限制維度</dt><dd>${h(row.constraint === 'requests' ? '請求數' : row.constraint === 'tokens' ? 'token 數' : '未知')}</dd></div><div><dt>原窗口 / 容量政策版本</dt><dd><code>${h(row.version || '未知')}</code><br><code>${h(row.policy_version || '未知')}</code></dd></div><div><dt>原限額設定版本</dt><dd><code>${h(row.configuration_version || '未知')}</code></dd></div></dl><p class="capacity-reason">${h(reason(row.reason_code))}<small class="mono">${h(row.reason_code || 'capacity_window_unknown')}</small></p><p class="metadata">${historical ? '此處保留原事件，包含已經過的窗口；目前狀態不回填或改寫這筆紀錄。' : '時間經過只解除這項容量時間限制，不自動恢復健康熔斷、補造資格或保證下次可派送。'} 未知時間不等於立即可用，窗口與 API 費用分別保存。</p></div></details>`;
  }

  function windowsPanel() {
    const dispatch = data?.dispatch, windows = dispatch?.windows;
    if (!dispatch || !windows) return '';
    if (windows.schema_version !== 'capacity-windows-v1') return '<section class="panel capacity-windows" id="capacity-windows"><h2>限流窗口</h2><p class="metadata">尚無可確認的窗口資料；不據此推定限制已解除，派送仍依伺服器原閘門。</p></section>';
    const groups = Array.isArray(windows.groups) ? windows.groups : [], events = Array.isArray(windows.events) ? windows.events : [];
    const pendingKnown = Array.isArray(windows.pending_sources);
    const hasPending = pendingKnown && windows.pending_sources.length > 0 || Number.isSafeInteger(windows.pending_source_count) && windows.pending_source_count > 0;
    const groupCount = Number.isSafeInteger(windows.active_group_count) && windows.active_group_count >= 0 ? windows.active_group_count : null;
    const groupCoverage = windows.active_group_history_complete === false || groupCount !== null && groupCount > groups.length ? '本頁不是完整清單，還有生效群組未列出；不能由本頁推定其他群組可派送。' : windows.active_group_history_complete === true ? '本頁涵蓋這次快照列出的生效群組；新派送仍需伺服器核對。' : '來源未提供清單完整性，不依本頁推定其他群組可派送。';
    const policy = (Array.isArray(dispatch.registered_policies) ? dispatch.registered_policies : []).find(row => row.version === dispatch.active_version);
    const adoption = preservationUnverified() ? '以下保留原窗口政策，目前可派送狀態無法核對' : !dispatch.active_version ? '新任務未啟用容量政策' : !policy || !Object.hasOwn(policy, 'windows_version') ? '目前政策的窗口採用狀態尚未提供' : policy.windows_version === windows.version ? '新任務已採用登錄窗口版本' : policy.windows_version ? '目前政策與窗口後端版本不同，不能推定已採用' : '目前政策未採用窗口檢查';
    return `<section class="panel capacity-windows" id="capacity-windows"><div class="capacity-dispatch-heading"><h2>限流窗口與原始回應</h2><span class="badge ${groups.length || hasPending || !pendingKnown ? 'amber' : 'blue'}">${hasPending ? '有待匯入或未知來源' : !pendingKnown ? '受控來源狀態尚未提供' : groups.length ? '本頁列有適用限制' : '本頁沒有列出的生效窗口'}</span></div><p>${h(adoption)}。後端版本 <code>${h(windows.version || '未知')}</code>；修訂 ${h(shown(windows.revision))}。</p><p class="metadata">只讀原始回應或受控保存來源，不重試、不探測、不自動更新來源。沒有列出的窗口不代表健康、資格或額度充足。</p><p class="metadata" data-window-coverage>生效群組總數：${h(shown(groupCount))}；本頁 ${groups.length} 筆。${h(groupCoverage)}</p>${pendingSources(windows)}<div class="capacity-window-grid">${groups.length ? groups.map(row => windowCard(row)).join('') : '<p class="metadata">本頁沒有列出生效群組；新派送仍須由伺服器核對全部適用限制，不能由此推定可用。</p>'}</div><details id="capacity-window-history" data-capacity-detail="window-history"><summary>查看原始限流窗口歷史</summary><p class="metadata">${Number.isSafeInteger(windows.event_count) ? `共 ${h(windows.event_count)} 筆；` : '保存總數未知；'}本頁 ${events.length} 筆${windows.event_history_complete === false ? '，較早事件未完整列出' : ''}。原來源時間與版本固定保存。</p><div class="capacity-window-grid">${events.length ? events.map(row => windowCard(row, true)).join('') : '<p class="metadata">尚無可確認的原窗口事件。</p>'}</div></details></section>`;
  }

  function pendingSources(windows) {
    const sources = Array.isArray(windows.pending_sources) ? windows.pending_sources : null;
    const count = Number.isSafeInteger(windows.pending_source_count) ? windows.pending_source_count : null;
    return `<details id="capacity-pending-sources" data-capacity-detail="pending-window-sources"><summary>查看受控來源的待匯入狀態</summary><p class="metadata">待匯入或尚無法確認的來源共 ${h(shown(count))} 筆；本頁 ${sources?.length ?? '未知'} 筆。只有明示更新來源才匯入原保存資料；GET／輪詢不匯入、不建立新的來源時間。這些限制只適用原登錄範圍，不推定其他候選可派送。</p>${sources === null ? '<p class="capacity-warning">未提供受控來源匯入狀態，不由缺值推定已匯入。</p>' : sources.length ? sources.map(source => `<article class="capacity-window capacity-pending-source" data-capacity-pending-source="${h(source.id)}"><h3>${source.status === 'pending' ? '尚未明示匯入' : '匯入狀態無法確認'}</h3><p class="capacity-reason">${h(reason(source.reason_code))}<small class="mono">${h(source.reason_code)}</small></p><dl><div><dt>原窗口 / 限額設定版本</dt><dd><code>${h(source.window_version || '未知')}</code><br><code>${h(source.configuration_version || '未知')}</code></dd></div><div><dt>受控來源安全識別</dt><dd><code>${h(source.source_id || '未知')}</code></dd></div><div><dt>原適用範圍安全識別</dt><dd><code>${h(source.physical || '未知')}</code></dd></div><div><dt>來源時間</dt><dd>原時間尚未匯入；不以本次查詢時間替代。</dd></div></dl></article>`).join('') : '<p class="metadata">這次快照沒有列出的待匯入來源；新的派送仍依伺服器原閘門判定。</p>'}</details>`;
  }

  function windowDecisions() {
    if (data?.dispatch?.windows?.schema_version !== 'capacity-windows-v1') return '';
    const tasks = Array.isArray(historyTasks) ? historyTasks.slice(0, 30) : [];
    const transitions = { initial: '首次實際派送', availability_fallback: '可用性備援：原已送出請求受限或不可用，沿原規則使用同池候選。',
      quality_recovery: '公開驗收未通過或未知後的品質補救；是否完成仍依原公開驗收。' };
    const triggers = { initial: '首次候選檢查', availability_failure: '已送出後的供應商可用性故障',
      public_validation_failed: '公開驗收未通過', public_validation_unavailable: '公開驗收結果未知' };
    const states = { dispatched: '已實際派送', excluded_before_dispatch: '派送前排除 · 未增加模型嘗試',
      considered_not_selected: '已檢查，未選用', unknown: '原候選證據無法確認' };
    const taskStates = { passed: '公開驗收通過', failed: '公開驗收未通過', unverifiable: '公開驗收未知',
      bound: '已接受', ready: '等待派送', running: '執行中', queued: '排隊中', waiting: '等待原准入條件',
      accounting_interrupted: '費用待核，停止派送', engineering_interrupted: '執行中斷，待查核',
      capacity_denied: '容量派送已拒絕', policy_denied: '原執行政策拒絕', expired: '任務已到期',
      cancelled: '任務已取消', cancel_requested: '已請求取消，結果仍待確認' };
    const note = historyState === 'stale' ? '<p class="capacity-warning" role="status">任務理由本次讀取失敗，保留上次保存的理由；沒有重新判定候選。</p>' : historyState === 'unavailable' ? '<p class="capacity-warning" role="status">任務理由目前無法讀取，不能推定候選沒有被排除。</p>' : '';
    return `<section class="panel capacity-windows" id="capacity-window-decisions"><h2>原任務的候選排除與備援理由</h2><p class="metadata">只讀任務當時保存的選擇。派送前排除與已送出後的 429／5xx 分開；可用性備援不等於公開驗收通過或內容品質改善。本頁列出最多 30 筆任務。</p>${note}${tasks.length ? tasks.map(task => {
      const decision = task.operator_observation?.task_decision;
      const attempts = Array.isArray(decision?.attempts) ? decision.attempts : [];
      const stages = Array.isArray(decision?.stages) ? decision.stages : [];
      return `<details class="capacity-window-task" data-window-task="${h(task.client + ':' + task.run)}" data-window-task-status="${h(task.status)}" data-capacity-detail="${h('window-task:' + task.client + ':' + task.run)}"><summary><span><strong>${h(task.run)}</strong><small>${h(task.client)} · ${h(taskStates[task.status] || '原任務狀態未知')}</small></span><span class="badge">${decision?.status === 'available' ? '原決策已保存' : decision?.status === 'partial' ? '原證據部分可讀' : '原決策尚無法確認'}</span></summary><div class="capacity-window-body">${attempts.length ? `<ol class="capacity-window-attempts">${attempts.map(attempt => `<li data-window-transition="${h(attempt.transition)}"><strong>${h(transitions[attempt.transition] || '原派送轉換原因未知')}</strong><br><code>${h(attempt.configuration?.model_version || '精確模型未知')}</code> · ${h(attempt.configuration?.provider_name || '供應商未知')} · ${attempt.passed === true ? '公開驗收通過' : attempt.passed === false ? '公開驗收未通過' : '公開驗收未知'}</li>`).join('')}</ol>` : '<p class="metadata">尚無可核對的實際派送轉換；不能由缺值推定零次或已成功備援。</p>'}${stages.map(stage => stage.state === 'not_entered' && stage.reason === 'public_validation_passed' ? '<section class="capacity-window-stage"><h3>公開驗收已通過，未進入後續選池</h3><p class="metadata">原任務不需要這段後續候選檢查，沒有新的模型嘗試或窗口判定。</p></section>' : `<section class="capacity-window-stage"><h3>${h(triggers[stage.trigger] || '原檢查觸發原因未知')}</h3>${stage.state !== 'recorded' ? '<p class="capacity-warning">這段原決策尚無法完整核對。</p>' : ''}${Array.isArray(stage.candidates) && stage.candidates.length ? `<div class="table-wrap"><table><thead><tr><th>原候選 / 精確設定</th><th>原選擇狀態</th><th>保存的窗口檢查與排除原因</th></tr></thead><tbody>${stage.candidates.map(candidate => {
        const window = candidate.capacity_window;
        return `<tr data-window-candidate="${h(candidate.config_id)}"><td><code>${h(candidate.configuration?.model_version || '未知')}</code><br>${h(candidate.configuration?.provider_name || '未知')}<br><small>${h(candidate.config_id || '設定識別未知')}</small></td><td>${h(states[candidate.state] || '原狀態未知')}</td><td>${window ? `<span data-window-candidate-status="${h(window.status)}">${h(window.status === 'wait' ? '原窗口要求等待' : window.status === 'open' ? '當時窗口未排除' : window.status === 'not_applicable' ? '當時未採用窗口檢查' : '原窗口未知，不能放行')}</span><br>原最早可再評估：${h(stamp(window.not_before))}<br><code>${h(window.reason_code || 'capacity_window_unknown')}</code>${window.evidence_refs?.length ? `<br><small>原證據：${window.evidence_refs.map(value => h(value)).join('、')}</small>` : ''}` : '<span class="metadata">未提供可核對的原窗口檢查；不以目前窗口回填。</span>'}${(candidate.reasons || []).map(code => `<p class="capacity-reason">${h(reasons[code] || '原候選依當時規則排除；安全代碼保留供查核。')}<small class="mono">${h(code)}</small></p>`).join('')}</td></tr>`;
      }).join('')}</tbody></table></div>` : '<p class="metadata">原逐候選證據尚未提供。</p>'}</section>`).join('')}</div></details>`;
    }).join('') : '<p class="metadata">尚無可讀的原任務候選紀錄。</p>'}</section>`;
  }

  function preservationUnverified() {
    const integrity = data?.dispatch?.integrity;
    return integrity?.protocol === 'capacity-preservation-v1' && integrity.status !== 'verified';
  }

  function preservationPanel() {
    const integrity = data?.dispatch?.integrity;
    if (!integrity || integrity.protocol !== 'capacity-preservation-v1') return '';
    const unverified = preservationUnverified();
    const count = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : '未知';
    const scope = integrity.affected_groups === null ? '無法確認受影響範圍；不能從下方清單推定任何名額可用。' :
      Array.isArray(integrity.affected_groups) && integrity.affected_groups.length ? '已識別受影響的群組共 ' + integrity.affected_groups.length + ' 筆；其他群組仍由服務核對全部原閘門。' :
      '這次保存狀態沒有列出受影響群組；不代表供應商容量或健康已獲保證。';
    const safeReason = ['capacity_store_integrity_unavailable', 'capacity_store_scope_unavailable'].includes(integrity.reason_code) ?
      '<code>' + h(integrity.reason_code) + '</code>' : '';
    return '<section class="panel capacity-integrity" id="capacity-integrity" aria-labelledby="capacity-integrity-heading">' +
      '<div class="capacity-dispatch-heading"><h2 id="capacity-integrity-heading">' +
      (unverified ? '容量狀態無法核對' : '容量保存狀態可核對') + '</h2><span class="badge ' +
      (unverified ? 'amber' : 'blue') + '">' + (unverified ? '目前可用名額未知' : '保存資料核對通過') + '</span></div>' +
      '<p>' + (unverified ? '下方原保存資料保留原時間、政策與證據，不能據此推定目前名額可用或重新派送。已有回應、已知費用與未決名額分別保留。' :
        '本次可核對保存狀態；新派送仍須通過原授權、資格、健康、期限與費用檢查。') + '</p><p class="metadata">' +
      h(scope) + '</p>' + safeReason + '<dl class="capacity-preservation-properties">' +
      '<div><dt>保存修訂</dt><dd>' + h(count(integrity.revision)) + '</dd></div>' +
      '<div><dt>保存中繼資料筆數</dt><dd>' + h(count(integrity.metadata_rows)) + '</dd></div>' +
      '<div><dt>受影響任務筆數</dt><dd>' + h(count(integrity.affected_task_count)) + '</dd></div></dl>' +
      '<p class="metadata">查詢不修復、不重送、不釋放名額；程序退出不代表供應商停止或退款。保存政策與真實供應商容量資格分開。</p></section>';
  }

  function dispatchUsage(dispatch) {
    const groups = Array.isArray(dispatch.groups) ? dispatch.groups : [];
    const globallyUnknown = preservationUnverified() && dispatch.integrity.affected_groups === null;
    const counters = [['rpm', 'RPM · 保守請求計數', 'rpm_used'], ['tpm', 'TPM · 保守 token 預留', 'tpm_reserved'],
      ['concurrency', '目前占用並行名額', 'concurrency_used']];
    return '<div class="capacity-usage-grid">' + groups.map(group => {
      const currentUnknown = globallyUnknown || group.observation_status === 'unavailable';
      return '<article class="capacity-usage" data-capacity-usage="' + h(group.group) + '"><h3>' + h(group.group) +
      '</h3><p class="metadata">' + h(scopes[group.scope?.kind] || '範圍未知') + ' · ' + h(group.scope?.ref) + '</p><dl>' +
      counters.map(([key, label, field]) => '<div><dt>' + h(label) + '</dt><dd data-capacity-counter="' + key + '">' +
        h(currentUnknown ? '未知' : shown(group[field])) + '</dd></div>').join('') + '</dl>' +
      (currentUnknown ? '<details data-capacity-detail="' + h('preserved-usage:' + group.group) +
        '"><summary>查看原保存的保守計數</summary><p class="metadata">以下是原保存資料，不是目前可用名額。原來源時間與事件見原紀錄。</p><dl>' +
        counters.map(([key, label, field]) => '<div><dt>' + h(label) + '</dt><dd data-capacity-original-counter="' + key + '">' +
          h(shown(group[field])) + '</dd></div>').join('') + '</dl></details>' : '') + '</article>';
    }).join('') +
      '</div><p class="metadata">' + (globallyUnknown || groups.some(group => group.observation_status === 'unavailable') ? '標示未知的群組目前無法核對；即使原保存值為零，也不能推定名額已釋放或可派送。其他群組仍由服務核對各自閘門。' :
        groups.length ? '以上為本機保守計數與占用，包含未送出的預留、已送出但結果未明的占用，以及完成後仍保留的保守窗口；不代表供應商實際用量、剩餘額度或重置時間保證。未決名額不會因時間或程序消失自動釋放。' :
          '尚無保存的逐次容量計數；不能由缺少紀錄推定供應商容量充足。') + '</p>';
  }

  function admissionHistory(dispatch) {
    const events = Array.isArray(dispatch.events) ? dispatch.events : [];
    const allocations = Array.isArray(dispatch.allocations) ? dispatch.allocations : [];
    const kinds = { activation: '政策變更', prepared: '送出前已預留', sent: '已送出，等待回應', complete: '回應已保存',
      uncertain: '結果不明，保留名額', released: '送出前已釋放', refused: '容量拒絕', bound_violation: '用量上界不符，停止後續派送' };
    const summary = (label, count, complete, total) => `${label}：${Number.isSafeInteger(total) && total >= count ? '共 ' + total + ' 筆，' : '保存總數未提供；'}本頁 ${count} 筆${complete === false ? '，還有較早紀錄未列出' : ''}；容量紀錄與 API 費用、客戶用量費分別保存。`;
    return `<details id="capacity-admission-history" data-capacity-detail="admission-history"><summary>查看逐次容量准入與拒絕歷史</summary><p class="metadata">${h(summary('事件', events.length, dispatch.event_history_complete, dispatch.event_count))} 僅呈現原事件的版本、時間與安全原因；目前政策不回填歷史。</p>${events.length ? `<div class="table-wrap"><table><thead><tr><th>順序 / 原時間</th><th>原政策版本 / 狀態</th><th>保存原因 / 容量紀錄</th></tr></thead><tbody>${events.map(event => {
      const allocation = allocations.find(item => item.id === event.allocation);
      const version = Object.hasOwn(event, 'version') ? event.version : allocation?.policy_version;
      return `<tr data-capacity-event="${h(event.kind)}"><td>${h(shown(event.sequence))}<br>${h(stamp(event.at))}</td><td><code>${h(version === null ? '未啟用' : version || '原版本未提供')}</code><br>${h(kinds[event.kind] || '原事件狀態未知')}</td><td>${event.reason_code ? `${h(reasons[event.reason_code] || '容量判定無法確認，原安全代碼保留供查核。')}<br><code>${h(event.reason_code)}</code>` : '沒有額外拒絕原因'}${event.allocation ? `<br><code>${h(event.allocation)}</code>` : ''}</td></tr>`;
    }).join('')}</tbody></table></div>` : '<p class="metadata">尚未提供容量事件歷史。</p>'}</details><details data-capacity-detail="allocations"><summary>查看保存的容量預留紀錄</summary><p class="metadata">${h(summary('預留紀錄', allocations.length, dispatch.allocation_history_complete, dispatch.allocation_count))} 釋放只代表已確認未送出，不代表供應商退款。</p>${allocations.length ? `<div class="table-wrap"><table><thead><tr><th>容量紀錄 / 原政策</th><th>狀態 / 所有適用群組</th><th>實際請求對應</th></tr></thead><tbody>${allocations.map(item => `<tr data-capacity-allocation="${h(item.id)}"><td><code>${h(item.id)}</code><br><code>${h(item.policy_version)}</code></td><td>${h(kinds[item.state] || '原狀態未知')}<br>${(item.groups || []).map(group => h(group.group)).join('、')}</td><td><code>${h(item.wire_sha256)}</code><br>${h(item.serializer)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="metadata">尚未提供容量預留紀錄；不能由此推定沒有未決名額。</p>'}</details>`;
  }

  function dispatchPanel() {
    const dispatch = data?.dispatch;
    if (!dispatch) return '<p class="capacity-warning">容量執行政策尚未啟用；保留現有同步派送與滿載拒絕規則。</p>';
    const policies = Array.isArray(dispatch.registered_policies) ? dispatch.registered_policies : [];
    const active = dispatch.active_version;
    if (policySelection === null || policySelection && !policies.some(policy => policy.version === policySelection)) {
      policySelection = active || '';
    }
    const disabled = mutationBusy || readState !== 'current' || preservationUnverified() || !Number.isSafeInteger(dispatch.revision);
    return `<section class="panel capacity-dispatch" id="capacity-dispatch"><div class="capacity-dispatch-heading"><h2>逐次派送容量政策</h2><span class="badge ${preservationUnverified() || !active ? 'amber' : 'blue'}" id="capacity-dispatch-status">${preservationUnverified() ? '原啟用狀態待核對' : active ? '已明示啟用' : '尚未啟用'}</span></div><p>${preservationUnverified() ? '以下保留原設定與啟用紀錄；目前容量狀態無法核對，不能從原版本推定新任務可派送。' : active ? '新任務依接受時固定的容量政策，在每次實際送出前檢查所有適用群組；容量可用仍須通過原品質、健康、資料位置及費用閘門。' : '容量執行政策尚未啟用；保留現有同步派送與滿載拒絕規則。'}</p><dl class="capacity-policy-summary"><div><dt>${preservationUnverified() ? '原保存政策版本' : '目前政策版本'}</dt><dd class="mono" id="capacity-active-version">${h(active || '未啟用')}</dd></div><div><dt>${preservationUnverified() ? '原保存修訂' : '設定修訂'}</dt><dd id="capacity-policy-revision">${h(dispatch.revision)}</dd></div><div><dt>請求協定</dt><dd class="mono">${h(dispatch.protocol)}</dd></div></dl><div class="capacity-policy-controls"><label for="capacity-policy-version">新任務的容量政策<select id="capacity-policy-version" ${disabled || !policies.length ? 'disabled' : ''}><option value="" ${policySelection === '' ? 'selected' : ''}>停用新任務容量政策</option>${policies.map(policy => `<option value="${h(policy.version)}" ${policySelection === policy.version ? 'selected' : ''}>${h(policy.version)}</option>`).join('')}</select></label><button class="button" id="capacity-activate-policy" ${disabled || policySelection === (active || '') || !policies.length ? 'disabled' : ''}>明示套用容量政策</button></div><p class="metadata">只能選用伺服器已登錄版本；頁面不能編輯 key、群組或上限。停用與切換不得放寬仍有效的容量占用；既有任務保留原政策及期限。</p>${policies.length ? `<details data-capacity-detail="policies"><summary>查看已登錄版本與適用設定</summary><ul class="capacity-history">${policies.map(policy => `<li><code>${h(policy.version)}</code> · 限額設定 <code>${h(policy.configuration_version)}</code> · 原任務最長 ${h(shown(policy.max_seconds))} 秒</li>`).join('')}</ul></details>` : '<p class="capacity-warning">尚無已登錄容量政策，沒有可啟用的版本。</p>'}${activationError ? `<p class="capacity-warning" role="status" id="capacity-activation-error">${h(activationError)}</p>` : ''}${dispatchUsage(dispatch)}${admissionHistory(dispatch)}</section>`;
  }

  function activatePolicy() {
    if (mutationBusy || readState !== 'current' || preservationUnverified() || !data?.dispatch) return;
    const version = policySelection || null, revision = data.dispatch.revision;
    if (version !== null && !data.dispatch.registered_policies.some(policy => policy.version === version)) return;
    context.confirmAction('明示變更新任務容量政策', `<p>採用版本：<strong>${h(version || '停用')}</strong>；設定修訂 ${h(revision)}。</p><p>不修改 key、群組或限額。既有任務保留原政策與期限；仍有效的窗口用量或未決名額會阻止切換，失敗不自動重試。</p>`, async () => {
      const startedGeneration = generation;
      mutationBusy = true; activationError = ''; render();
      try {
        const next = await context.api('/monitor/capacity/activate', { revision, version });
        if (generation !== startedGeneration) return;
        requestVersion += 1; flight = null;
        data = { ...data, dispatch: next }; policySelection = next.active_version || '';
        await refresh(true);
      } catch (error) {
        if (generation !== startedGeneration) return;
        activationError = reasons[error.message] || '容量政策變更未確認，請重新讀取後查核；未自動重試。';
        await refresh(true);
        throw Error(activationError);
      } finally {
        if (generation === startedGeneration) { mutationBusy = false; render(); }
      }
    }, '容量政策已保存；既有任務保留原版本與期限。');
  }

  function limit(item, kind) {
    const observations = item.observations || [];
    return `<article class="capacity-limit" data-capacity-dimension="${h(kind + ':' + item.dimension)}"><div class="capacity-limit-title"><strong>${h(dimensions[item.dimension])}</strong><span class="badge ${item.status === 'known' ? 'blue' : 'amber'}">${h(statuses[item.status] || '無法確認')}</span></div><div class="capacity-value"><span>${h(shown(item.value))}</span><small>${h(item.unit)}</small></div><dl><div><dt>原窗口</dt><dd>${item.window_seconds === null ? '未提供固定秒數' : h(item.window_seconds) + ' 秒'}</dd></div><div><dt>適用範圍</dt><dd>${h(scopes[item.scope.kind])} · ${h(item.scope.ref)}</dd></div><div><dt>觀測時間</dt><dd>${h(stamp(item.observed_at))}</dd></div><div><dt>重置時間</dt><dd>${h(stamp(item.reset_at))}${item.reset_period ? ' · ' + h(period(item.reset_period)) : ''}</dd></div><div><dt>有效至</dt><dd>${h(stamp(item.valid_until))}<br>${item.valid_until_basis === "local_freshness" ? "本機資料時效，非供應商保證" : item.valid_until_basis === "controlled_import" ? "受控匯入的適用效期" : "效期依據未知"}</dd></div>${item.dimension === 'usd' ? `<div><dt>原來源剩餘 / 用量</dt><dd>${h(shown(item.remaining))} / ${h(shown(item.usage))} USD</dd></div><div><dt>來源 BYOK inclusion flag</dt><dd>${h(shown(item.include_byok_in_limit))}；不代表外部 BYOK 供應商費用上界</dd></div>` : ''}</dl>${(item.reason_codes || []).map(code => `<p class="capacity-reason">${h(reason(code))}<small class="mono">${h(code)}</small></p>`).join('')}${observations.length ? `<p class="metadata">觀測依據：${observations.map(value => h(value.evidence_ref)).join('、')}</p>` : ''}${item.status !== 'known' && observations.some(value => value.value !== null) ? `<p class="metadata">保留的原始觀測值：${observations.map(value => h(shown(value.value)) + ' ' + h(value.unit)).join('、')}；未當作目前可用量。</p>` : ''}</article>`;
  }

  function group(group) {
    const last = group.last_refresh;
    const unavailable = group.observation_status === 'unavailable';
    return `<details class="panel capacity-group" data-capacity-group="${h(group.id)}"><summary><span><strong>${h(group.id)}</strong><small>${h(scopes[group.scope.kind])} · ${h(group.scope.ref)}</small></span><span class="badge ${unavailable ? 'amber' : ''}">${h(unavailable ? '本次無法核對，保留原資料' : sources[group.source.kind])}</span></summary><div class="capacity-group-body">${unavailable ? '<p class="capacity-warning">本次群組保存狀態無法核對；以下是原保存來源、原觀測時間及上限，不當作目前可用名額。未修復、重抓或恢復健康。</p>' : ''}<p class="metadata">來源版本：<span class="mono">${h(group.source.version)}</span>${group.source.kind === 'controlled_import' ? ' · 由受控設定匯入，未宣稱自動取得' : ''}</p>${last ? `<p class="capacity-refresh-status">最近明示更新：${h(stamp(last.attempted_at))} · ${last.status === 'available' ? '資料已保存' : h(reason(last.error_code))}</p>` : '<p class="metadata">尚未執行明示來源更新。</p>'}${group.retained_after_refresh_failure ? '<p class="capacity-warning">本次更新失敗，保留上次可信資料及原觀測時間；未自動重試。</p>' : ''}<h2>${unavailable ? '原保存供應商限制' : '供應商限制'}</h2><div class="capacity-dimensions">${group.supplier_limits.map(item => limit(item, 'supplier')).join('')}</div><h2>${unavailable ? '原保存本機保守上限' : '本機保守上限'}</h2><p class="metadata">本機設定與供應商剩餘量分別保存；本頁不授予派送名額。</p><div class="capacity-dimensions">${group.local_limits.map(item => limit(item, 'local')).join('')}</div>${group.refresh_history?.length ? `<p class="metadata">保存 ${h(group.refresh_history_count)} 次明示更新；本頁列出最近 ${h(group.refresh_history.length)} 次。</p><ul class="capacity-history">${group.refresh_history.map(item => `<li>${h(stamp(item.attempted_at))} · ${item.status === 'available' ? '已保存' : h(reason(item.error_code))} · ${h(item.request_count)} 次來源請求</li>`).join('')}</ul>` : ''}</div></details>`;
  }

  function render() {
    if (!root?.querySelector('#capacity-page')) return;
    const detailKey = node => node.dataset.capacityGroup ? 'group:' + node.dataset.capacityGroup : node.dataset.capacityDetail;
    const opened = new Set(Array.from(root.querySelectorAll('details[open]')).map(detailKey));
    const active = document.activeElement;
    const focusedDetail = active?.tagName === 'SUMMARY' ? detailKey(active.closest('details')) : null;
    const focusId = root.contains(active) ? active.id : null;
    const issue = readState === 'stale' ? '<p class="capacity-warning" role="status">本次讀取失敗，保留上次畫面與原始時間。</p>' : readState === 'unavailable' ? '<p class="capacity-warning" role="status">容量資料目前無法讀取；未派送或更新來源。</p>' : '';
    root.innerHTML = `<div id="capacity-page"><div class="page-heading"><div><div class="eyebrow">SUPPLIER CAPACITY</div><h1>供應商限額來源</h1><p>分開查看供應商限制、本機上限及未知資料。來源更新只在操作員明示確認後執行。</p></div><button class="button" id="capacity-refresh-sources" ${!data?.configured || mutationBusy || preservationUnverified() ? 'disabled' : ''}>明示更新限額來源</button></div>${context.demo ? '<p class="capacity-warning">合成驗收環境 · 未建立真實供應商容量、吞吐或 SLO 資格。</p>' : ''}${issue}${preservationPanel()}${dispatchPanel()}${windowsPanel()}${windowDecisions()}${!data ? '<div class="panel empty">正在讀取已保存的容量資料…</div>' : !data.configured ? '<div class="panel empty">尚未配置供應商容量來源，沒有自動查詢或可用容量承諾。</div>' : `<p class="metadata">設定版本：<span class="mono">${h(data.configuration_version)}</span></p><section class="panel capacity-bindings" id="capacity-bindings"><h2>伺服器綁定與共用群組</h2><p class="metadata">不同 credential reference 可共用同一帳號限額。頁面不能新增 key、解除群組或擴大上限。</p><div class="table-wrap"><table><thead><tr><th>綁定</th><th>供應商 / 帳號</th><th>Credential reference / 端點</th><th>所有適用群組</th></tr></thead><tbody>${data.bindings.map(binding => `<tr><td class="mono">${h(binding.id)}</td><td>${h(binding.provider)}<br>${h(binding.account_ref)}</td><td>${h(binding.credential_ref)}<br>${h(binding.endpoint_ref)}</td><td>${binding.groups.map(value => `<span class="badge">${h(value)}</span>`).join(' ')}</td></tr>`).join('')}</tbody></table></div></section><div class="capacity-groups">${data.groups.map(group).join('')}</div>`}</div>`;
    root.querySelectorAll('details').forEach(node => { node.open = opened.has(detailKey(node)); });
    const target = focusedDetail ? Array.from(root.querySelectorAll('details')).find(node => detailKey(node) === focusedDetail)?.querySelector('summary') : focusId ? root.querySelector('#' + CSS.escape(focusId)) : null;
    if (focusedDetail || focusId) {
      const fallback = [target, document.getElementById('reload'), root.querySelector('#capacity-policy-version'), root.querySelector('#capacity-refresh-sources')].find(node => node && !node.disabled) || root.querySelector('#capacity-page');
      if (fallback.id === 'capacity-page') fallback.tabIndex = -1;
      fallback.focus({ preventScroll: fallback === target });
    }
    const selector = root.querySelector('#capacity-policy-version');
    if (selector) selector.onchange = event => { policySelection = event.target.value; activationError = ''; render(); };
    const activation = root.querySelector('#capacity-activate-policy');
    if (activation) activation.onclick = activatePolicy;
    root.querySelector('#capacity-refresh-sources').onclick = () => context.confirmAction('明示更新供應商限額來源',
      `<p>使用伺服器配置的來源，在固定時間與請求數上限內查詢 key 限額。${data?.dispatch?.windows?.schema_version === 'capacity-windows-v1' ? '也會匯入伺服器登錄的受控保存來源，保留原觀測時間、版本與證據；一般查詢不匯入。' : ''}不讀取帳號 credits、不發送模型請求，也不修改客戶帳務或派送政策。失敗的來源保留原資料與原時間。</p>`,
      async () => { await context.api('/monitor/capacity/refresh', {}); await refresh(true); }, '限額來源查核已完成；詳情與未知事項見各群組。');
  }

  function refresh(force = false) {
    if (!context || !root?.querySelector('#capacity-page')) return Promise.resolve();
    if (flight && !force) return flight;
    const currentGeneration = generation, currentRequest = ++requestVersion;
    const pending = (async () => {
      try {
        const next = await context.api('/monitor/capacity');
        let tasks = null, taskRead = 'idle';
        if (next?.dispatch?.windows?.schema_version === 'capacity-windows-v1') {
          try {
            const snapshot = await context.api('/monitor/snapshot');
            if (!Array.isArray(snapshot.tasks)) throw Error('Saved task projection unavailable');
            tasks = snapshot.tasks; taskRead = 'current';
          } catch { taskRead = 'failed'; }
        }
        if (generation !== currentGeneration || currentRequest !== requestVersion || !root?.querySelector('#capacity-page')) return;
        data = next; readState = 'current';
        if (taskRead === 'failed') historyState = historyTasks ? 'stale' : 'unavailable';
        else { historyTasks = tasks; historyState = taskRead; }
        render();
      } catch {
        if (generation !== currentGeneration || currentRequest !== requestVersion || !root?.querySelector('#capacity-page')) return;
        readState = data ? 'stale' : 'unavailable'; render();
      } finally { if (flight === pending) flight = null; }
    })();
    flight = pending;
    return pending;
  }

  function mount(node, nextContext) {
    context = nextContext;
    if (root === node && root.querySelector('#capacity-page')) return;
    generation += 1; root = node; data = null; flight = null; readState = 'idle'; policySelection = null; mutationBusy = false; activationError = ''; historyTasks = null; historyState = 'idle';
    root.innerHTML = '<div id="capacity-page"></div>';
    render(); refresh();
  }
  function resetView() { generation += 1; requestVersion += 1; root = null; context = null; data = null; flight = null; readState = 'idle'; policySelection = null; mutationBusy = false; activationError = ''; historyTasks = null; historyState = 'idle'; }
  setInterval(() => refresh(), 5000);
  return { mount, refresh, resetView };
})();
