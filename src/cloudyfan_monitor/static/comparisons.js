'use strict';
window.CloudyFanComparisons = (() => {
  const policy = 'original-policy-reproduction-v1';
  // V1 historical read ceilings stay fixed when current creation limits shrink.
  const readCeilings = Object.freeze({max_tasks:20,max_source_files:256,max_source_bytes:4194304,max_source_file_bytes:1048576,
    max_candidate_configurations:12,max_decision_points:9,max_output_bytes:262144,
    max_policy_compute_milliseconds:250,worker_cpu_seconds:2,worker_wall_seconds:5});
  const names = {reproduced:['已重現決定','blue'],partial:['部分可重現','amber'],unknown:['未知','amber'],
    not_provided:['未提供','amber'],not_evaluable:['不可評估','amber']};
  const modes = {cloudyfan:'CloudyFan MRC',fixed_high:'全大模型優先',low_first:'全便宜模型優先',open_source_first:'全開源模型優先'};
  const tiers = {open_source:'開源池',low:'低分池',mid:'中分池',high:'高分池'};
  const statuses = {passed:'公開驗收通過',failed:'未通過',unknown:'未知',unverifiable:'無法確認',queued:'排隊中',
    running:'執行中',ready:'已準備',bound:'已凍結',expired:'已到期',engineering_interrupted:'工程中斷',
    accounting_interrupted:'費用待核',policy_denied:'執行條件不符',capacity_denied:'容量條件不符',retired:'已停止原存取',task_busy:'執行中'};
  const actions = {direct:'直接選擇',classify:'重套原分類規則',stop:'停止',advance_quality_stage:'轉至下一品質階段'};
  // Only server-registered strategy names; the browser never assembles a policy.
  const strategies = {'qcs-direct-v1':'QCS 一律直接選模','qcs-classify-v1':'QCS 有校準時一律付費分類'};
  const candidateStates = {diverged:['與原決定分歧','amber'],same_as_original:['與原決定相同','blue'],
    not_evaluable:['不可評估','amber'],not_adoptable:['不可採用','amber']};
  const candidateRows = {evaluated:'已推演',not_evaluable:'不可評估',not_adoptable:'不可採用',not_observed:'未觀測'};
  const candidateReasons = {same_as_original_outside_decision_point:'此步不是直接／分類判斷點，與原決定相同。',
    registered_direct_choice:'依註冊策略直接選模。',registered_classification:'依註冊策略付費分類。',
    exact_classifier_calibration_missing:'缺少精確分類校準；不等於分類沒有效益。',
    classification_estimate_unknown:'分類費或完整流程估計未保存，無法推演。',
    classification_exceeds_speed_limit:'分類流程超出原速度上限；不放寬原限制。',
    single_eligible_candidate:'只有一個合格候選，沒有分類可選。',original_decision_not_reproduced:'原決定無法由保存輸入重現，不能作為比較基準。',
    after_first_divergence:'已在第一個分歧點停止；之後的流程沒有替代結果。',
    original_decision_inputs_unavailable:'原後續決定所需的保存輸入不足。'};
  const stops = {requires_unsaved_alternative_answer:'需要未保存的替代模型答案，推演在此停止。',
    requires_unsaved_classifier_reply:'需要從未取得的 Jev 分類回覆，推演在此停止。'};
  const stages = {initial:'首個決定',same_pool:'同池備援',low_mid:'低／中候選比較',mid_high:'中／高候選比較',after_validation:'原公開驗收之後'};
  const reasons = {saved_inputs_missing:'缺少當時的保存輸入。',historical_evidence_unavailable:'原歷史依據無法核對。',
    saved_selection_unverifiable:'保存的選擇與依據無法核對。',source_expired:'原文已到期，只使用合法保留的中繼資料。',
    source_hash_mismatch:'保存資料摘要無法核對。',unsupported_source:'此保存來源尚不支援重現。',
    task_not_completed:'原任務尚未完成，必要決策依據不足。',comparison_limit_reached:'已達本次處理上限，保留未完成範圍。',
    saved_policy_values_invalid:'保存的政策值不符合原契約。',unsupported_policy_version:'此原政策版本尚不支援重現。',
    original_binding_unavailable:'原任務綁定無法核對。',original_context_unavailable:'缺少原決策時值。',
    candidate_scope_incomplete:'原候選集合不完整，不能改用目前候選補齊。',candidate_gate_evidence_unavailable:'原候選閘門依據不足。',
    original_economics_unavailable:'缺少原成本、速度或分類校準依據。',classifier_evidence_unavailable:'缺少原分類回覆的合法保存依據。',
    classifier_binding_mismatch:'原分類回覆與任務／契約對應無法核對。',policy_inputs_inconsistent:'原政策輸入彼此不一致。',
    original_unknown_charge:'原費用未明，按原規則停止；比較不核帳或重送。',
    same_pool_availability_or_initial:'依原模式與同池可用性及排序規則選擇。',original_mode_initial_tier:'按原模式選擇首個層級。',
    no_eligible_candidate:'當時沒有符合原條件的候選。',public_validation_passed:'原保存的公開驗收已通過，按原規則停止。',
    generation_call_limit:'已達原作答請求上限。',provider_rate_limited:'原供應商限額使此路徑停止。',
    same_pool_exhausted:'原同池候選已用盡，轉至下一品質階段。',quality_stage_limit:'已達原品質階段上限。',
    generation_attempt_limit:'已達原作答嘗試上限。',original_baseline_high_recovery:'原基線規則在未通過後選用高層候選。',
    original_sequential_classifier:'按原循序政策進入分類規則。',exact_classifier_calibration_missing:'原精確分類校準未提供；不能推論分類無效。',
    single_eligible_candidate:'當時只有一個符合原條件的候選。',classification_estimate_unknown:'原分類費用或選擇分布估計未知。',
    classified_flow_estimate_unknown:'原分類完整流程耗時估計未知。',classification_exceeds_speed_limit:'原分類估計超過當時速度限制。',
    classification_not_economic:'原估計未達分類淨節省門檻；不表示分類無效。',
    conservative_net_saving_positive:'原保存估計符合分類淨節省門檻；不是已實現節費。',
    parse_failure:'原分類回覆無法解析，按原規則採用較高層。',distribution_missing:'原分類分布缺漏，按原規則採用較高層。',
    explicit_abstention_choice:'原分類明示棄權，按原規則採用較高層。',
    tied_or_inconsistent_distribution:'原分類分布未形成一致選擇，按原規則採用較高層。',reported_choice_applied:'依已保存的原分類回覆規則採用其選擇。',
    original_decision_inputs_unavailable:'原後續決定所需的保存輸入不足。',original_evidence_unavailable:'原保存證據無法核對。',
    original_data_permission_unavailable:'原資料使用權限無法核對。',original_data_clock_unavailable:'原資料存取時間無法核對。',
    original_raw_access_expired:'原文存取已到期；只使用合法保留的安全中繼資料。',original_contract_unavailable:'原執行契約無法核對。',
    comparison_worker_timeout:'比較處理已達時間上限，保留未完成範圍。',comparison_worker_interrupted:'比較處理中斷，保留未完成範圍。',
    comparison_compute_limit:'比較計算已達本次上限，保留未完成範圍。',comparison_output_limit:'比較輸出已達本次上限，保留未完成範圍。'};
  const failures = {unauthorized:'操作員授權已失效，請重新認證。',comparison_not_found:'這份比較報告目前不存在或無法取得。',
    comparison_evidence_unavailable:'保存的比較報告目前無法核對；未重新計算。',
    comparison_report_not_ready:'這份比較仍在準備，尚無可確認的保存結果。',
    comparison_busy:'另一個比較仍在處理，請稍後明示重試。',comparison_store_unavailable:'比較儲存目前無法使用。',
    comparison_scope_invalid:'選取範圍已無法核對，請重新選擇1至20件不同任務。',
    comparison_policy_not_registered:'原政策版本尚未註冊，請重新讀取可用版本。',
    comparison_strategy_not_registered:'候選策略尚未註冊，請重新讀取可用策略。',
    unsupported_comparison_fields:'比較請求欄位不符合註冊介面，本次未確認完成。'};
  const countNames = {requested:'選定件數',evaluated:'已評估',reproduced:'已重現決定',partial:'部分可重現',unknown:'未知',
    not_evaluable:'不可評估',not_provided:'未提供',original_failed:'原任務未通過',original_interrupted:'原任務中斷',unknown_fee_tasks:'費用未明任務'};
  let root, context, generation = 0, catalogVersion = 0, reportVersion = 0;
  let catalog = null, catalogState = 'idle', catalogFlight = null, reportFlight = null;
  let selectedPolicy = '', selectedStrategy = '', selectedReport = '', selectedTasks = new Map(), reports = new Map(), reportState = 'idle';
  let applying = false, notice = '', filter = '';
  const key = task => JSON.stringify([task.client,task.run_id]);
  const id = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(value);
  const optionalId = value => value === null || id(value);
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
  const comparisonId = value => typeof value === 'string' && /^cmp-[a-f0-9]{32}$/.test(value);
  const identity = value => !!value && typeof value.client === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.client)
    && typeof value.run_id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(value.run_id);
  const amount = value => value === null || typeof value === 'string' && value.length <= 80 && /^\d+(?:\.\d+)?$/.test(value);
  const signed = value => value === null || typeof value === 'string' && value.length <= 80 && /^-?\d+(?:\.\d+)?$/.test(value);
  const span = value => value === null || !!value && Object.keys(value).length === 2 && amount(value.lower) && amount(value.upper)
    && value.lower !== null && value.upper !== null;
  function candidateProjection(value, strategy) {
    if (!value || value.strategy !== strategy || !Object.hasOwn(candidateStates,value.status) || value.adopted !== false
        || value.scope !== 'first_divergence_from_saved_inputs' || !Array.isArray(value.reasons) || value.reasons.length > 32
        || value.reasons.some(code => !id(code)) || !Array.isArray(value.decisions) || value.decisions.length > readCeilings.max_decision_points) return false;
    return value.decisions.every(row => integer(row.index) && optionalId(row.stage) && Object.hasOwn(candidateRows,row.status) && id(row.reason)
      && (row.status === 'not_observed' || row.reason === 'original_decision_inputs_unavailable' && row.status === 'not_evaluable'
        && Object.keys(row).length === 4
        || optionalId(row.action) && optionalId(row.selected_tier) && [true,false,null].includes(row.diverged) && optionalId(row.stop)
          && (row.original === null || !!row.original && optionalId(row.original.action) && optionalId(row.original.selected_tier))
          && !!row.forecast && span(row.forecast.complete_flow_usd) && span(row.forecast.classifier_fee_usd)
          && signed(row.forecast.net_saving_lower_usd) && optionalId(row.forecast.calibration)
          && (row.forecast.calibration_contract_sha256 === null || digest(row.forecast.calibration_contract_sha256))));
  }
  const knownLimits = value => value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === Object.keys(readCeilings).length
    && Object.entries(readCeilings).every(([name,bound]) => Object.hasOwn(value,name)
      && Number.isSafeInteger(value[name]) && value[name] > 0 && value[name] <= bound);
  const h = value => context.esc(value);
  const mounted = () => !!root?.querySelector('#comparisons-page');
  const money = value => value === null || value === undefined ? '未知' : '$' + h(value);
  const stamp = value => finite(value) ? new Date(value*1000).toLocaleString('zh-TW',{hour12:false}) : '未提供';
  const reason = code => Object.hasOwn(reasons,code) ? reasons[code] : '保存依據不足或不適用，這一步無法確認。';
  const current = (version, actionContext) => generation === version && context === actionContext && mounted();
  function catalogue(value) {
    if (!value || value.schema_version !== 'saved-policy-comparison-catalog-v1' || !knownLimits(value.limits)
        || !Array.isArray(value.registered_policies) || value.registered_policies.length > 1
        || value.registered_policies.some(row => row?.version !== policy)
        || !Array.isArray(value.candidate_strategies) || value.candidate_strategies.some(row => !Object.hasOwn(strategies,row?.version))
        || !Array.isArray(value.task_scopes) || value.task_scopes.length > 2000
        || typeof value.task_catalog_incomplete !== 'boolean' || !Array.isArray(value.reports) || value.reports.length > 2000) return null;
    if (value.task_scopes.some(row => !identity(row) || !optionalId(row.mode) || !optionalId(row.category) || !finite(row.created_at))
        || new Set(value.task_scopes.map(key)).size !== value.task_scopes.length
        || value.reports.some(row => !comparisonId(row.comparison_id) || !['completed','incomplete'].includes(row.status)
          || !finite(row.created_at) || row.policy_version !== policy || !integer(row.requested_tasks) || row.requested_tasks > readCeilings.max_tasks)
        || new Set(value.reports.map(row => row.comparison_id)).size !== value.reports.length) return null;
    return value;
  }
  function reportProjection(value) {
    if (!value || value.schema_version !== 'saved-policy-comparison-v1' || !comparisonId(value.comparison_id)
        || !['completed','incomplete'].includes(value.status) || value.policy_version !== policy || !id(value.computation_version)
        || !finite(value.created_at) || !finite(value.frozen_at) || !knownLimits(value.limits)
        || ![value.data_sha256,value.policy_sha256,value.result_sha256].every(digest)
        || !Array.isArray(value.scope?.tasks) || value.scope.tasks.length < 1 || value.scope.tasks.length > value.limits.max_tasks
        || value.scope.tasks.some(task => !identity(task)) || new Set(value.scope.tasks.map(key)).size !== value.scope.tasks.length
        || !value.counts || Object.keys(countNames).some(name => !integer(value.counts[name]) || value.counts[name] > value.limits.max_tasks)
        || value.counts.requested !== value.scope.tasks.length || !Array.isArray(value.items) || value.items.length > value.scope.tasks.length
        || Object.hasOwn(value.scope,'candidate_strategy') && !Object.hasOwn(strategies,value.scope.candidate_strategy)) return null;
    const strategy = value.scope.candidate_strategy;
    if (value.items.some(item => strategy ? !candidateProjection(item.candidate,strategy) : Object.hasOwn(item,'candidate'))) return null;
    const scope = new Set(value.scope.tasks.map(key)), seen = new Set();
    for (const item of value.items) {
      const original = item.original, reproduced = item.reproduction, costs = original?.costs, charge = costs?.customer_usage_charge;
      if (!identity(item.task) || !scope.has(key(item.task)) || seen.has(key(item.task)) || !original || !id(original.task_status)
          || !['mode','category','pool_version','contract_id','contract_version','routing_version'].every(name => optionalId(original[name]))
          || !(original.created_at === null || finite(original.created_at)) || !Array.isArray(original.path) || original.path.length > readCeilings.max_decision_points
          || original.path.some(row => !integer(row.index) || !optionalId(row.tier) || !optionalId(row.config_id)
            || !(row.configuration_sha256 === null || digest(row.configuration_sha256)) || !['passed','failed','unknown'].includes(row.public_status) || !optionalId(row.stop))
          || !costs || !amount(costs.known_api_usd) || !amount(costs.complete_api_usd)
          || !(costs.unknown_events === null || integer(costs.unknown_events)) || !charge
          || !['available','not_provided','unknown'].includes(charge.status) || charge.basis !== 'original_customer_ledger'
          || !amount(charge.known_usd) || !amount(charge.complete_usd) || !(charge.unknown_events === null || integer(charge.unknown_events))
          || charge.status === 'not_provided' && [charge.known_usd,charge.complete_usd,charge.unknown_events].some(value => value !== null)
          || !reproduced || !Object.hasOwn(names,reproduced.status) || reproduced.scope !== 'decision_from_saved_inputs'
          || reproduced.adopted !== false || reproduced.financial_admissibility !== 'not_replayed'
          || !Array.isArray(reproduced.reasons) || reproduced.reasons.length > 32 || reproduced.reasons.some(code => !id(code))
          || !Array.isArray(reproduced.decisions) || reproduced.decisions.length > readCeilings.max_decision_points
          || reproduced.decisions.some(row => !integer(row.index) || !optionalId(row.stage) || !optionalId(row.action)
            || !optionalId(row.selected_tier) || !optionalId(row.config_id) || !optionalId(row.reason) || ![true,false,null].includes(row.match))
          || !Array.isArray(item.evidence) || item.evidence.length > readCeilings.max_source_files
          || item.evidence.some(row => !id(row.kind) || !digest(row.sha256) || !integer(row.bytes))) return null;
      seen.add(key(item.task));
    }
    return value;
  }
  function remember() {
    const open = new Set(Array.from(root.querySelectorAll('details[open]')).map(node => node.dataset.comparisonDetail));
    const active = document.activeElement, focus = root.contains(active) ? {id:active.id,
      detail:active.closest('details')?.dataset.comparisonDetail,start:active.selectionStart,end:active.selectionEnd} : null;
    return {open,focus};
  }
  function restore(view) {
    root.querySelectorAll('details').forEach(node => {node.open = view.open.has(node.dataset.comparisonDetail);});
    if (!view.focus) return;
    const target = view.focus.id ? root.querySelector('#'+CSS.escape(view.focus.id))
      : Array.from(root.querySelectorAll('details')).find(node => node.dataset.comparisonDetail === view.focus.detail)?.querySelector('summary');
    const fallback = target && !target.disabled ? target : root.querySelector('#comparison-report-select') || root;
    if (fallback === root) root.tabIndex = -1;
    fallback.focus({preventScroll:!!target});
    if (typeof view.focus.start === 'number' && target?.setSelectionRange) target.setSelectionRange(view.focus.start,view.focus.end);
  }
  function candidateView(candidate) {
    const state = candidateStates[candidate.status];
    const range = value => value ? `$${h(value.lower)}–$${h(value.upper)}` : '未知';
    return `<section class="comparison-candidate" data-candidate-strategy="${h(candidate.strategy)}"><h4>候選策略推演 · ${h(strategies[candidate.strategy])} <span class="badge ${state[1]}" data-candidate-status="${h(candidate.status)}">${state[0]}</span></h4><ol>${candidate.decisions.map(row => `<li data-candidate-row="${h(row.index)}"><strong>${h(candidateRows[row.status])}</strong> · ${h(stages[row.stage] || '階段未提供')}${row.action ? ` · ${h(actions[row.action] || '動作未提供')}` : ''}${row.selected_tier ? ` · ${h(tiers[row.selected_tier] || '層級未知')}` : ''}${row.original ? `<br><span class="metadata">原決定：${h(actions[row.original.action] || '未提供')}${row.original.selected_tier ? ` · ${h(tiers[row.original.selected_tier] || '層級未知')}` : ''} · ${row.diverged === true ? '與原決定不同' : row.diverged === false ? '與原決定相同' : '無法對應'}</span>` : ''}<br><span class="metadata">${h(candidateReasons[row.reason] || '保存依據不足或不適用，這一步無法推演。')}</span>${row.stop ? `<br><span class="metadata">${h(stops[row.stop] || '推演在此停止。')}</span>` : ''}${row.forecast && (row.forecast.complete_flow_usd || row.forecast.classifier_fee_usd) ? `<br><span class="metadata">原成本模型預測（非實付）：完整流程 ${range(row.forecast.complete_flow_usd)} · 分類費估計 ${range(row.forecast.classifier_fee_usd)} · 保守淨節省 ${row.forecast.net_saving_lower_usd === null ? '未知' : '$' + h(row.forecast.net_saving_lower_usd)} · 校準 ${h(row.forecast.calibration ?? '未知')}</span>` : ''}</li>`).join('') || '<li>沒有可推演的保存決定。</li>'}</ol><p class="metadata">只推演保存輸入能確定的步驟，到第一個需要未保存答案或回覆的分歧點停止；不報替代成功率、勝率或真實節省，也不自動採用。</p></section>`;
  }
  function itemView(item) {
    const original = item.original, reproduced = item.reproduction, charge = original.costs.customer_usage_charge;
    const title = names[reproduced.status];
    return `<article class="panel comparison-item" data-comparison-task="${h(key(item.task))}"><div class="panel-header"><div><h3 class="mono">${h(item.task.run_id)}</h3><p>客戶端 ${h(item.task.client)}</p></div><span class="badge ${title[1]}" data-reproduction-status="${h(reproduced.status)}">${title[0]}</span></div><div class="panel-body"><div class="comparison-columns"><section><h4>原實際路徑</h4><p>${h(statuses[original.task_status] || '原任務狀態未知')} · ${h(modes[original.mode] || '模式未提供')}</p>${original.path.length ? `<ol>${original.path.map(row => `<li>${h(tiers[row.tier] || '層級未提供')} · ${h(statuses[row.public_status] || '未知')}${row.config_id ? ` <code>${h(row.config_id)}</code>` : ''}</li>`).join('')}</ol>` : '<p class="metadata">原路徑未提供；不代表沒有執行或費用為零。</p>'}</section><section><h4>由保存輸入重現的決定</h4>${reproduced.decisions.length ? `<ol>${reproduced.decisions.map(row => `<li><strong>${h(actions[row.action] || '動作未提供')}</strong> · ${h(stages[row.stage] || '階段未提供')}${row.selected_tier ? ` · ${h(tiers[row.selected_tier] || '層級未知')}` : ''}${row.config_id ? ` <code>${h(row.config_id)}</code>` : ''}<br><span class="metadata">${h(reason(row.reason))}</span><br><span class="metadata">${row.match === true ? '與原選擇相符' : row.match === false ? '與原選擇不同' : '與原選擇尚無法對應'}</span></li>`).join('')}</ol>` : '<p class="metadata">沒有足夠資料重現決定。</p>'}<p class="metadata">只重套已保存的決策規則；未重做完整准入或費用檢查，未核准新派送。</p></section></div>${item.candidate ? candidateView(item.candidate) : ''}<dl class="comparison-costs"><div><dt>原已知 API 費用</dt><dd>${money(original.costs.known_api_usd)}</dd></div><div><dt>原完整 API 費用</dt><dd>${money(original.costs.complete_api_usd)}</dd></div><div><dt>費用未明事件</dt><dd>${h(original.costs.unknown_events ?? '未知')}</dd></div><div><dt>原客戶用量費</dt><dd>${charge.status === 'not_provided' ? '未提供' : money(charge.complete_usd)}</dd></div></dl><p class="metadata">客戶帳本已知 ${charge.status === 'not_provided' ? '未提供' : money(charge.known_usd)} · 待核 ${h(charge.unknown_events ?? (charge.status === 'not_provided' ? '未提供' : '未知'))}；以上僅列原權威紀錄，不用公開單價重算。</p><details data-comparison-detail="${h(key(item.task))}"><summary>查看缺漏、原版本與證據摘要</summary><ul>${reproduced.reasons.map(code => `<li>${h(reason(code))}</li>`).join('') || '<li>此項未列缺漏原因；重現決定不代表替代模型成果。</li>'}</ul><dl class="comparison-facts"><div><dt>原類別</dt><dd>${h(original.category ?? '未提供')}</dd></div><div><dt>原契約</dt><dd>${h(original.contract_id ?? '未提供')} · ${h(original.contract_version ?? '未提供')}</dd></div><div><dt>原模型池版本</dt><dd class="mono">${h(original.pool_version ?? '未提供')}</dd></div><div><dt>原路由版本</dt><dd class="mono">${h(original.routing_version ?? '未提供')}</dd></div><div><dt>原建立時間</dt><dd>${h(original.created_at === null ? '未提供' : stamp(original.created_at))}</dd></div></dl>${item.evidence.length ? `<ul class="comparison-hashes">${item.evidence.map(row => `<li><code>${h(row.kind)}</code> · ${h(row.bytes)} bytes<br><code>${h(row.sha256)}</code></li>`).join('')}</ul>` : '<p class="metadata">未提供可核對的證據摘要。</p>'}</details></div></article>`;
  }
  function reportView() {
    const saved = reports.get(selectedReport)?.value;
    if (!selectedReport) return '<div class="panel empty">選擇已保存的比較報告，或明示開始一次原政策重現。</div>';
    const issue = reportState === 'current' ? '' : `<p class="comparison-warning" role="status">${h(notice || '本次報告尚未確認；若有前次內容，僅保留作為先前觀測。')}</p>`;
    if (!saved) return issue + '<div class="panel empty">尚未取得這份報告；未使用其他報告代替。</div>';
    return `${issue}<section class="panel comparison-report" data-comparison-id="${h(saved.comparison_id)}"><div class="panel-header"><h2>已保存的比較報告</h2><span class="badge ${saved.status === 'incomplete' ? 'amber' : 'blue'}">${saved.status === 'incomplete' ? '計算未完整' : '計算完成'}</span></div><div class="panel-body"><p class="mono">${h(saved.comparison_id)}</p><p>原政策重現 · ${h(saved.policy_version)}</p><p class="metadata">凍結 ${h(stamp(saved.frozen_at))} · 保存 ${h(stamp(saved.created_at))} · 計算版 ${h(saved.computation_version)}</p><div class="comparison-counts">${Object.entries(countNames).map(([name,label]) => `<div><span>${label}</span><b data-comparison-count="${name}">${h(saved.counts[name])}</b></div>`).join('')}</div>${saved.status === 'incomplete' ? '<p class="comparison-warning">部分範圍未完成；已列項目不代表全部選定任務。</p>' : ''}<details data-comparison-detail="report-hashes"><summary>查看不可變報告摘要</summary><dl class="comparison-facts">${[['資料',saved.data_sha256],['政策',saved.policy_sha256],['結果',saved.result_sha256]].map(([label,value]) => `<div><dt>${label} SHA256</dt><dd class="mono">${h(value)}</dd></div>`).join('')}</dl></details></div></section>${saved.items.map(itemView).join('')}`;
  }
  function render() {
    if (!mounted()) return;
    const view = remember(), available = new Set(catalog?.task_scopes.map(key) || []);
    const rows = catalog?.task_scopes.filter(task => (task.client+' '+task.run_id).toLowerCase().includes(filter.toLowerCase())) || [];
    const maximum = catalog?.limits.max_tasks ?? null;
    const maximumLabel = maximum === null ? '上限待確認' : `最多${maximum}件`;
    const canStart = catalogState === 'current' && !applying && selectedPolicy === policy
      && (selectedStrategy === '' || catalog.candidate_strategies.some(row => row.version === selectedStrategy))
      && selectedTasks.size > 0 && selectedTasks.size <= maximum && [...selectedTasks.keys()].every(value => available.has(value));
    const reportRows = [...(catalog?.reports || [])];
    if (selectedReport && !reportRows.some(row => row.comparison_id === selectedReport)) reportRows.unshift({comparison_id:selectedReport});
    root.innerHTML = `<div id="comparisons-page"><div class="page-heading"><div><div class="eyebrow">SAVED-EVIDENCE COMPARISON</div><h1>保存證據比較</h1><p>明示選取合法保存的範圍，只重現原政策可由當時輸入支持的決定。</p></div></div><p class="notice">比較不執行模型、工具或公開驗收，不改原任務、模式、費率或期限，也不啟用政策。沒有替代結果，不報勝率或真實節省。</p>${context.demo ? '<p class="metadata comparison-synthetic">合成驗收環境 · 接線結果不代表真實模型能力、省費或正式採用。</p>' : ''}${catalogState !== 'current' ? '<p class="comparison-warning" role="status">任務範圍與政策清單尚未確認；保留選取，開始比較暫停。</p>' : ''}<section class="panel comparison-create"><div class="panel-header"><h2>明示開始原政策重現</h2><span class="metadata">${maximumLabel} · 有界處理</span></div><div class="panel-body"><label for="comparison-policy">已註冊的原政策</label><select id="comparison-policy" ${catalogState === 'current' && !applying ? '' : 'disabled'}><option value="">請明示選擇原政策</option>${catalog?.registered_policies.length ? `<option value="${policy}" ${selectedPolicy === policy ? 'selected' : ''}>原政策重現 v1 · ${policy}</option>` : ''}</select><label for="comparison-strategy">候選策略（選填）</label><select id="comparison-strategy" ${catalogState === 'current' && !applying ? '' : 'disabled'}><option value="">不加入候選策略</option>${(catalog?.candidate_strategies || []).map(row => `<option value="${h(row.version)}" ${selectedStrategy === row.version ? 'selected' : ''}>${h(strategies[row.version])} · ${h(row.version)}</option>`).join('')}</select><p class="metadata">候選策略只由伺服器註冊；推演到第一個分歧點即停止，不啟用策略。</p><label for="comparison-task-filter">篩選保存的任務身分</label><input type="search" id="comparison-task-filter" value="${h(filter)}" autocomplete="off"><p class="metadata">清單${catalog?.task_catalog_incomplete ? '僅列部分合法保存範圍；不代表全部歷史' : '由伺服器解析合法保存範圍'}。選取不取得派送名額或新增扣款。</p><fieldset class="comparison-scope" ${applying ? 'disabled' : ''}><legend>選擇任務 · 已選 <span id="comparison-selected-count">${selectedTasks.size}</span> / ${maximum ?? '待確認'}</legend>${rows.map(task => `<label class="comparison-task"><input id="comparison-task-${h(encodeURIComponent(key(task)))}" type="checkbox" data-comparison-select="${h(key(task))}" ${selectedTasks.has(key(task)) ? 'checked' : ''}><span><b class="mono">${h(task.run_id)}</b><br>${h(task.client)} · ${h(modes[task.mode] || '模式未提供')}</span></label>`).join('') || '<p class="metadata">尚無可選取的任務身分。</p>'}${[...selectedTasks].filter(([value]) => !available.has(value)).map(([value,task]) => `<label class="comparison-task comparison-warning"><input type="checkbox" data-comparison-select="${h(value)}" checked><span>${h(task.client)} / ${h(task.run_id)} · 目前不在可選範圍，請明示取消或重新讀取。</span></label>`).join('')}</fieldset><div class="comparison-actions"><button id="comparison-start" class="button primary" type="button" ${canStart ? '' : 'disabled'}>${applying ? '正在保存比較…' : '開始原政策重現'}</button><button id="comparison-clear" class="button" type="button" ${applying || !selectedTasks.size ? 'disabled' : ''}>清除選取</button></div><p class="metadata">超出保存或處理上限時保留不完整原因；計算完成不代表原任務成功或新派送已獲准。傳輸未確認時不自動重送。</p></div></section><section class="panel comparison-saved"><div class="panel-body"><label for="comparison-report-select">讀取已保存的比較</label><select id="comparison-report-select"><option value="">選擇報告</option>${reportRows.map(row => `<option value="${h(row.comparison_id)}" ${selectedReport === row.comparison_id ? 'selected' : ''}>${h(row.comparison_id)}${row.status ? ` · ${row.status === 'completed' ? '計算完成' : '計算未完整'}` : ' · 先前開啟'}</option>`).join('')}</select></div></section>${reportView()}</div>`;
    root.querySelector('#comparison-policy').onchange = event => {selectedPolicy = event.target.value;render();};
    root.querySelector('#comparison-strategy').onchange = event => {selectedStrategy = event.target.value;render();};
    root.querySelector('#comparison-task-filter').oninput = event => {filter = event.target.value;render();};
    root.querySelectorAll('[data-comparison-select]').forEach(node => {node.onchange = () => {
      const value = node.dataset.comparisonSelect, task = catalog?.task_scopes.find(item => key(item) === value);
      if (!node.checked) selectedTasks.delete(value);else if (task) selectedTasks.set(value,{client:task.client,run_id:task.run_id});
      render();
    };});
    root.querySelector('#comparison-clear').onclick = () => {selectedTasks.clear();render();};
    root.querySelector('#comparison-start').onclick = propose;
    root.querySelector('#comparison-report-select').onchange = event => {selectedReport = event.target.value;reportState = 'loading';notice = '';reportVersion += 1;render();void readReport(true);};
    restore(view);
  }
  function propose() {
    const tasks = [...selectedTasks.values()].map(task => ({...task})), version = generation, actionContext = context;
    const strategy = selectedStrategy;
    if (applying || catalogState !== 'current' || selectedPolicy !== policy || !tasks.length || tasks.length > catalog.limits.max_tasks
        || strategy && !catalog.candidate_strategies.some(row => row.version === strategy)) return;
    actionContext.confirmAction('開始原政策重現',`<p>原政策 <code>${policy}</code>${strategy ? ` · 候選策略 <code>${h(strategy)}</code>` : ''} · 選定 ${tasks.length} 件：</p><ul>${tasks.map(task => `<li>${h(task.client)} / ${h(task.run_id)}</li>`).join('')}</ul><p>伺服器將固定合法保存的證據與凍結時間，獨立保存一份比較；不派送、不核帳、不扣款或啟用政策。缺少原輸入維持未知，超出處理上限保留不完整結果。</p>`,async () => {
      if (!current(version,actionContext) || applying || catalogState !== 'current'
          || tasks.length > catalog.limits.max_tasks
          || tasks.some(task => !catalog.task_scopes.some(row => key(row) === key(task)))) throw Error('比較範圍或目前處理上限已變更，請調整選取後再確認。');
      applying = true;notice = '';catalogVersion += 1;const displayVersion = ++reportVersion;render();
      try {
        const value = await actionContext.api('/monitor/comparisons',{policy_version:policy,tasks,...(strategy ? {candidate_strategy:strategy} : {})});
        if (!current(version,actionContext)) return;
        const saved = reportProjection(value);
        if (!saved || JSON.stringify(saved.scope.tasks.map(key).sort()) !== JSON.stringify(tasks.map(key).sort())
            || (saved.scope.candidate_strategy ?? '') !== strategy) throw Error('comparison_projection_unconfirmed');
        const previous = reports.get(saved.comparison_id);
        if (previous && previous.bytes !== JSON.stringify(saved)) throw Error('comparison_projection_changed');
        reports.set(saved.comparison_id,{value:saved,bytes:JSON.stringify(saved)});
        if (displayVersion === reportVersion) {selectedReport = saved.comparison_id;reportState = 'current';}
        catalog.reports = [{comparison_id:saved.comparison_id,status:saved.status,created_at:saved.created_at,policy_version:policy,requested_tasks:tasks.length},
          ...catalog.reports.filter(row => row.comparison_id !== saved.comparison_id)];
      } catch (error) {
        if (!current(version,actionContext)) return;
        notice = Object.hasOwn(failures,error.code) ? failures[error.code] : '本次比較結果尚未確認，可能已保存；請查看已保存報告，勿自動重送。';
        reportState = 'stale';throw Error(notice);
      } finally {if (current(version,actionContext)) {applying = false;render();}}
    },'比較報告已獨立保存；未啟用政策或變更原任務。',false,'開始比較並保存報告');
  }
  function readCatalogue(force = false) {
    if (!context || !mounted() || applying) return Promise.resolve();
    if (catalogFlight && !force) return catalogFlight;
    const version = generation, actionContext = context, request = ++catalogVersion;
    const active = () => current(version,actionContext) && request === catalogVersion;
    const pending = (async () => {
      try {
        const value = catalogue(await actionContext.api('/monitor/comparisons'));
        if (!active()) return;
        if (!value) throw Error('comparison_catalogue_unconfirmed');
        const changed = JSON.stringify(catalog) !== JSON.stringify(value) || catalogState !== 'current';
        catalog = value;catalogState = 'current';if (changed) render();
      } catch {if (active()) {catalogState = catalog ? 'stale' : 'unavailable';render();}}
      finally {if (catalogFlight === pending) catalogFlight = null;}
    })();
    catalogFlight = pending;return pending;
  }
  function readReport(force = false) {
    if (!context || !mounted() || !selectedReport || applying && !force) return Promise.resolve();
    if (reportFlight && reportFlight.id === selectedReport && !force) return reportFlight.promise;
    const version = generation, actionContext = context, selected = selectedReport, request = ++reportVersion;
    const active = () => current(version,actionContext) && selectedReport === selected && request === reportVersion;
    const flight = {id:selected,promise:null};
    flight.promise = (async () => {
      try {
        const value = reportProjection(await actionContext.api('/monitor/comparisons/'+selected));
        if (!active()) return;
        const previous = reports.get(selected);
        if (!value || value.comparison_id !== selected || previous && previous.bytes !== JSON.stringify(value)) throw Error('comparison_saved_report_changed');
        const changed = !previous || reportState !== 'current' || notice;
        reports.set(selected,{value,bytes:JSON.stringify(value)});reportState = 'current';notice = '';if (changed) render();
      } catch (error) {if (active()) {reportState = 'stale';notice = Object.hasOwn(failures,error.code) ? failures[error.code] : '這份保存報告目前無法核對；保留前次觀測，未重新計算。';render();}}
      finally {if (reportFlight === flight) reportFlight = null;}
    })();
    reportFlight = flight;return flight.promise;
  }
  function refresh(force = false) {return Promise.all([readCatalogue(force),readReport(force)]);}
  function mount(node,nextContext) {
    if (context?.sessionVersion === nextContext.sessionVersion && root === node && mounted()) return;
    resetView();root = node;context = nextContext;root.innerHTML = '<div id="comparisons-page"></div>';render();void refresh();
  }
  function resetView() {
    generation += 1;catalogVersion += 1;reportVersion += 1;root = context = catalog = catalogFlight = reportFlight = null;
    catalogState = reportState = 'idle';selectedPolicy = selectedReport = notice = filter = '';applying = false;
    selectedStrategy = '';selectedTasks = new Map();reports = new Map();
  }
  setInterval(() => {void refresh();},5000);
  return {mount,refresh,resetView};
})();
