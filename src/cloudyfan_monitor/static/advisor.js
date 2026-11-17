'use strict';
// #80：路徑二（顧問先答）任務明細、換池紀錄頁、客戶端經驗共用開關。
// 只讀 Monitor 的公開投影；回復與開關都經確認視窗，由伺服器端操作員 token 保護。
window.CloudyFanAdvisor = (() => {
  const esc = (v) => String(v ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const badge = (text, tone='') => `<span class="badge ${tone}">${esc(text)}</span>`;
  const money = value => value === null || value === undefined ? '未知' : '$' + esc(value);
  const advisorModes = {answer:['顧問自答','green'], delegate:['顧問交出','blue'], incomplete:['顧問未完成','amber']};
  const endReasons = {passed:'通過驗收', attempt_limit:'用滿作答次數', recommendations_exhausted:'建議已用完',
    advisor_incomplete:'顧問未完成（不派送）'};
  const incompleteReasons = {invalid_json:'回覆不是 JSON', invalid_schema:'格式不符', too_few_recommendations:'建議少於 2 組',
    no_valid_recommendations:'建議全部作廢', provider_rejected:'供應商拒絕（429）'};
  const voidReasons = {malformed:'格式不對', not_in_pool:'不在池內', tier_mismatch:'層級不符', duplicate:'重複'};
  const sources = {advisor_answer:'顧問自答', delegated:'改派'};
  const roles = {advisor:'顧問', judge:'裁判', answer:'作答'};
  const tiers = {open_source:'開源池', low:'低分池', mid:'中分池', high:'高分池'};
  const kinds = {auto_change:'自動換池', rollback:'回復', auto_switch:'總開關', suppressed:'已關閉而未換', no_change:'未換（有告警或被擋）'};
  const moveReasons = {significant_checkup_gain:'體檢顯著較好', significant_checkup_loss:'體檢顯著較差',
    significant_checkup_drop:'降智暫停', fee_bound_unconfirmed:'缺費用上界，未移動'};
  const failures = {auto_pool_changed_reload:'快照已變更，請重新讀取後再回復。', auto_pool_rollback_unavailable:'沒有可回復的換池紀錄，或上一份快照已過期。',
    auto_pool_not_configured:'主服務尚未接上自動換池。', experience_not_configured:'主服務尚未接上契約經驗庫。'};

  // ---- 任務明細：路徑二 ----
  function callRows(calls) {
    return calls.map(call => `<tr><td>${esc(roles[call.role] || call.role)}</td><td class="mono">${esc(call.model)}</td><td class="mono">${esc(call.generation_id)}</td><td class="amount">${money(call.cost_usd)}</td></tr>`).join('');
  }
  function traceSection(trace) {
    const advisor = trace && typeof trace.advisor === 'object' && trace.advisor ? trace.advisor : null;
    if (!advisor) return '';
    const [label, tone] = advisorModes[advisor.advisor_mode] || ['顧問決定未確認', 'amber'];
    const costs = advisor.costs || {};
    const attempts = Array.isArray(trace.attempts) ? trace.attempts : [];
    const recs = (advisor.recommendations || []).map(r => `<li>第 ${esc((r.rank ?? 0) + 1)} 順位 · <span class="mono">${esc(r.setting)}</span> · ${esc(tiers[r.tier] || r.tier)}</li>`).join('');
    const voided = (advisor.voided || []).map(v => `<li>第 ${esc((v.rank ?? 0) + 1)} 順位作廢：${esc(voidReasons[v.reason] || v.reason)} <code>${esc(v.reason)}</code></li>`).join('');
    const attemptRows = attempts.map(a => `<tr><td>${esc(a.index)}</td><td>${esc(sources[a.source] || a.source)}</td><td class="mono">${esc(a.setting || '—')}</td><td>${a.passed === true ? badge('通過','green') : a.passed === false ? badge('未通過','red') : badge('無法確認','amber')}</td><td class="amount">${a.calls && a.calls.length ? a.calls.map(c => money(c.cost_usd)).join(' + ') : '含在顧問呼叫'}</td></tr>`).join('');
    const allCalls = [...(advisor.calls || []), ...attempts.flatMap(a => a.calls || [])];
    return `<section class="trace-section advisor-trace" data-advisor-trace><div class="trace-section-title"><h3>路徑二：顧問先答</h3>${badge(label, tone)}</div>
<dl class="trace-facts"><div><dt>結束原因</dt><dd>${esc(endReasons[advisor.end_reason] || advisor.end_reason)}</dd></div>${advisor.incomplete_reason ? `<div><dt>未完成原因</dt><dd>${esc(incompleteReasons[advisor.incomplete_reason] || advisor.incomplete_reason)}</dd></div>` : ''}<div><dt>評分快照</dt><dd class="mono">${esc(advisor.score_snapshot)}</dd></div></dl>
<div class="advisor-columns"><div><h4>有效建議（依順位）</h4>${recs ? `<ol class="advisor-list">${recs}</ol>` : '<p class="muted">沒有有效建議</p>'}</div><div><h4>作廢建議</h4>${voided ? `<ul class="advisor-list">${voided}</ul>` : '<p class="muted">沒有作廢</p>'}</div></div>
<h4>各次作答</h4><div class="table-wrap"><table><thead><tr><th>#</th><th>來源</th><th>池內設定</th><th>驗收</th><th>費用</th></tr></thead><tbody>${attemptRows || '<tr><td colspan="5" class="muted">沒有作答</td></tr>'}</tbody></table></div>
<h4>各筆呼叫（含顧問／裁判）</h4><div class="table-wrap"><table><thead><tr><th>角色</th><th>模型</th><th>generation</th><th>費用</th></tr></thead><tbody>${callRows(allCalls) || '<tr><td colspan="4" class="muted">沒有呼叫</td></tr>'}</tbody></table></div>
<dl class="trace-facts advisor-costs"><div><dt>正式成本</dt><dd class="amount">${money(costs.formal_usd)}</dd></div><div><dt>研究全額</dt><dd class="amount">${money(costs.research_usd)}</dd></div><div><dt>顧問</dt><dd class="amount">${money(costs.advisor_usd)}</dd></div><div><dt>裁判</dt><dd class="amount">${money(costs.judge_usd)}</dd></div></dl>
<p class="metadata muted">正式成本不計設定中明示開源的顧問／裁判（零模型費用情境）；研究全額照收據全數列出。顧問的回答草稿與建議理由文字不在 Monitor 顯示。</p></section>`;
  }

  // ---- 換池紀錄頁 ----
  let poolRoot = null, poolContext = null, poolData = null, poolGeneration = 0;
  function moveText(row) {
    return (row.moves || []).map(m => `<span class="mono">${esc(m.config_id)}</span> ${esc(tiers[m.from] || m.from)} → ${esc(tiers[m.to] || m.to)}（${esc(moveReasons[m.reason] || m.reason)}）`).join('<br>')
      || (row.kind === 'auto_switch' ? (row.enabled ? '開啟自動換池' : '關閉自動換池') : row.kind === 'rollback' ? `回到 <span class="mono">${esc((row.restored_snapshot_id || '').slice(0, 12))}</span> 的分群` : '—');
  }
  function evidenceText(row) {
    const evidence = row.evidence || {};
    return Object.entries(evidence).map(([id, e]) => `<span class="mono">${esc(id)}</span> 分數 ${esc(e.score)}${e.confidence_interval ? `（區間 ${esc(e.confidence_interval.low)}–${esc(e.confidence_interval.high)}）` : ''} · ${esc(e.graded)} 題`).join('<br>') || '—';
  }
  function renderPool() {
    if (!poolRoot) return;
    const d = poolData;
    const heading = `<div class="page-heading"><div><div class="eyebrow">AUTO POOL</div><h1>換池紀錄</h1><p>依自家模型體檢自動調整高／中／低池。每次調整都是新快照，既有任務沿用舊快照；可回復上一份或關閉自動換池。</p></div></div>`;
    if (!d) { poolRoot.innerHTML = heading + '<div class="panel empty" role="status">正在讀取換池紀錄…</div>'; return; }
    if (!d.configured) { poolRoot.innerHTML = heading + '<div class="panel empty">主服務尚未接上自動換池；沒有紀錄可顯示。</div>'; return; }
    const grouping = ['open_source', 'low', 'mid', 'high'].map(tier => [tier, (d.current_grouping || {})[tier] || []]).map(([tier, ids]) => `<div><dt>${esc(tiers[tier] || tier)}${(d.disabled_tiers || []).includes(tier) ? ' ' + badge('已停用','red') : ''}</dt><dd class="mono">${ids.length ? ids.map(esc).join(', ') : '（空）'}</dd></div>`).join('');
    const rows = (d.changes || []).map(row => `<tr><td class="nowrap metadata">${esc(row.at)}</td><td>${esc(kinds[row.kind] || row.kind)}${(row.alerts || []).length ? '<br>' + badge('有告警','amber') : ''}</td><td class="mono">${esc((row.before_snapshot_id || row.snapshot_id || '').slice(0, 12))} → ${esc((row.after_snapshot_id || '').slice(0, 12) || '—')}</td><td>${moveText(row)}</td><td>${evidenceText(row)}</td><td>${esc(row.operator || '自動')}</td></tr>`).join('');
    poolRoot.innerHTML = heading + `<section class="panel"><div class="panel-header"><h2>目前快照</h2>${badge(d.auto_switch_enabled ? '自動換池：開' : '自動換池：關', d.auto_switch_enabled ? 'green' : 'amber')}</div><div class="panel-body"><p class="mono">${esc(d.current_snapshot_id)}</p><dl class="trace-facts">${grouping}</dl><div class="advisor-actions"><button class="button" id="auto-pool-rollback" type="button" ${d.rollback_available ? '' : 'disabled'}>回復上一份快照</button><button class="button" id="auto-pool-switch" type="button">${d.auto_switch_enabled ? '關閉自動換池' : '開啟自動換池'}</button></div><p class="metadata">關閉後體檢仍會記錄「本來會怎麼換」，但不發布新快照（包含降智暫停）。</p></div></section>
<section class="panel mt"><div class="panel-header"><h2>換池紀錄</h2>${badge('最新在上')}</div><div class="table-wrap"><table><thead><tr><th>時間</th><th>事件</th><th>前 → 後快照</th><th>移動與理由</th><th>體檢分數</th><th>操作者</th></tr></thead><tbody>${rows || '<tr><td colspan="6" class="muted">尚無紀錄</td></tr>'}</tbody></table></div></section>`;
    poolRoot.querySelector('#auto-pool-rollback')?.addEventListener('click', () => poolContext.confirmAction('回復上一份快照',
      '<p>以上一份快照的分群發布一份新快照；既有任務不受影響。</p>',
      () => poolContext.api('/monitor/auto-pool/rollback', {snapshot_id: d.current_snapshot_id}).catch(fail).then(refreshPool),
      '已回復；新任務使用回復後的分群。', false));
    poolRoot.querySelector('#auto-pool-switch')?.addEventListener('click', () => poolContext.confirmAction(d.auto_switch_enabled ? '關閉自動換池' : '開啟自動換池',
      '<p>只影響之後的體檢評估；現有快照與任務不變。</p>',
      () => poolContext.api('/monitor/auto-pool/auto-switch', {enabled: !d.auto_switch_enabled}).catch(fail).then(refreshPool),
      '總開關已更新。', false));
  }
  function fail(e) { throw Error(failures[e.code] || e.message); }
  async function refreshPool() {
    if (!poolRoot) return;
    const mine = ++poolGeneration;
    try { const next = await poolContext.api('/monitor/auto-pool'); if (mine === poolGeneration) { poolData = next; renderPool(); } }
    catch (e) { if (mine === poolGeneration) poolContext.error('換池紀錄未更新：' + (failures[e.code] || e.message)); }
  }
  function mountAutoPool(root, context) {
    poolRoot = root; poolContext = context; poolData = null; renderPool(); return refreshPool();
  }

  // ---- 客戶端設定：經驗「貢獻」「使用」 ----
  async function mountExperience(root, context) {
    if (!root) return;
    root.innerHTML = '<p class="muted">正在讀取經驗共用設定…</p>';
    let d;
    try { d = await context.api('/monitor/experience'); } catch (e) { root.innerHTML = `<p class="muted">經驗共用設定無法讀取：${esc(failures[e.code] || e.message)}</p>`; return; }
    if (!root.isConnected) return;
    if (!d.configured) { root.innerHTML = '<p class="muted">主服務尚未接上契約經驗庫；新客戶端預設「貢獻」「使用」都開。</p>'; return; }
    root.innerHTML = `<div class="table-wrap"><table><thead><tr><th>客戶端</th><th>貢獻經驗</th><th>使用共用經驗</th></tr></thead><tbody>${d.clients.map(c => `<tr><td>${esc(c.label)}<br><span class="mono muted">${esc(c.client)}</span></td>${['contribute', 'use'].map(name => `<td><label class="advisor-toggle"><input type="checkbox" data-experience-client="${esc(c.client)}" data-experience-name="${name}" ${c[name] ? 'checked' : ''}> ${c[name] ? '開' : '關'}</label></td>`).join('')}</tr>`).join('')}</tbody></table></div><p class="metadata">關閉「貢獻」會立即從共用層扣除該客戶端所有題目的支持；關閉「使用」只影響之後新建的題目能否拿到共用經驗。每題建立時凍結當下設定。</p>`;
    root.querySelectorAll('[data-experience-client]').forEach(input => input.addEventListener('change', event => {
      const client = input.dataset.experienceClient, name = input.dataset.experienceName, value = event.target.checked;
      event.target.checked = !value;
      context.confirmAction('變更經驗共用設定', `<p>${esc(client)} · ${name === 'contribute' ? '貢獻經驗' : '使用共用經驗'} → <strong>${value ? '開' : '關'}</strong></p>`,
        () => context.api('/monitor/experience/settings', {client, [name]: value}).catch(fail).then(() => mountExperience(root, context)),
        '經驗共用設定已保存。', false);
    }));
  }

  return {traceSection, mountAutoPool, refreshPool, mountExperience};
})();
