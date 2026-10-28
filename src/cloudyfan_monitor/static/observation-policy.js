'use strict';
window.CloudyFanObservationPolicy = (() => {
  let root, context, data = null, flight = null, generation = 0, requestVersion = 0;
  let readState = 'idle', selected = '', applying = false, notice = '';
  const registered = {
    'features-v1': {label:'特徵 v1', feature:'local-deterministic-v1', advice:null},
    'features-v2': {label:'特徵 v2', feature:'local-deterministic-v2', advice:null},
    'features-v2-advice-v1': {label:'特徵 v2 ＋ 觀察性建議 v1', feature:'local-deterministic-v2', advice:'route-recommendation-rules-v1'},
  };
  const failures = {
    configuration_changed_reload:'設定已由另一位操作員更新。本次未套用，請重新讀取後再確認。',
    observation_rule_not_registered:'這個規則版本尚未註冊。本次未套用，請重新讀取可用版本。',
    observation_policy_unavailable:'觀測規則設定目前無法核對。本次未確認套用，請重新讀取。',
    unauthorized:'操作員授權已失效，請重新認證。',
  };
  const h = value => context.esc(value);
  const mounted = () => !!root?.querySelector('#observation-policy-page');
  function projection(value) {
    if (!value || value.schema_version !== 'creation-observation-control-v1'
        || !Number.isSafeInteger(value.revision) || value.revision < 0
        || !['host','operator'].includes(value.configuration_source)
        || typeof value.capture_enabled !== 'boolean' || typeof value.recommendations_enabled !== 'boolean'
        || !Array.isArray(value.available_versions) || value.available_versions.length > 3) return null;
    const versions = value.available_versions.map(row => {
      const known = row && Object.hasOwn(registered,row.version) ? registered[row.version] : null;
      return known && row.feature_version === known.feature && row.recommendation_rule_version === known.advice ? row.version : null;
    });
    if (versions.some(version => !version) || new Set(versions).size !== versions.length
        || (value.active_version !== null && !versions.includes(value.active_version))) return null;
    if (value.configuration_source === 'operator') {
      if (value.capture_enabled !== (value.active_version !== null)
          || value.recommendations_enabled !== (value.active_version === 'features-v2-advice-v1')) return null;
    } else if (value.active_version !== null) return null;
    return {revision:value.revision,source:value.configuration_source,active:value.active_version,
      capture:value.capture_enabled,advice:value.recommendations_enabled,versions};
  }
  function versionLabel(version) {
    return version === null ? '停用新任務擷取與建議' : registered[version]?.label || '版本未確認';
  }
  function activeLabel() {
    return data.source === 'host' ? '尚未由操作員採用；沿用主機既有設定' : versionLabel(data.active);
  }
  function render() {
    if (!mounted()) return;
    const active = document.activeElement, focus = root.contains(active) ? active.id : null;
    const explanation = notice || (readState === 'stale' ? '本次讀取失敗，保留上次觀測；請重新讀取確認目前設定。'
      : readState === 'unavailable' ? '目前無法核對觀測規則，尚未取得可套用的版本。' : '');
    const current = readState === 'current' && !!data && !applying;
    const choices = data?.versions || [];
    if (!choices.includes(selected)) selected = '';
    root.innerHTML = `<div id="observation-policy-page"><div class="page-heading"><div><div class="eyebrow">CREATION-TIME OBSERVATIONS</div><h1>建立時觀測規則</h1><p>選擇之後新任務保存的特徵與觀察性路由建議；建議尚未採用，不改選模或路由。</p></div></div><p class="notice observation-policy-boundary">切換、停用與回退只影響新任務。已接受、排隊中、執行中及接續任務保留原版本；舊任務不重新擷取或補算。</p>${context.demo ? '<p class="metadata observation-policy-synthetic">合成驗收環境 · 不代表真實模型能力、分類成效或已實現節費。</p>' : ''}${explanation ? `<p class="observation-policy-warning" role="status">${h(explanation)}</p>` : ''}${!data ? '<div class="panel empty" role="status">正在讀取觀測規則；無法讀取時不推定擷取已停用。</div>' : `<section class="panel observation-policy-current" aria-labelledby="observation-policy-current-title"><div class="panel-header"><h2 id="observation-policy-current-title">新任務目前設定</h2><span class="badge ${readState === 'current' ? 'blue' : 'amber'}">${readState === 'current' ? '已讀取' : '待重新確認'}</span></div><div class="panel-body"><p id="observation-policy-active" data-observation-policy-active="${h(data.active || data.source)}">${h(activeLabel())}</p><dl class="observation-policy-facts"><div><dt>設定來源</dt><dd>${data.source === 'host' ? '主機既有設定；尚未明示採用規則版本' : '操作員明示採用'}</dd></div><div><dt>設定修訂</dt><dd id="observation-policy-revision" class="mono">${h(data.revision)}</dd></div><div><dt>新任務特徵擷取</dt><dd>${data.capture ? '已啟用' : '已停用'}</dd></div><div><dt>新任務觀察性建議</dt><dd>${data.advice ? '已啟用 · 未採用路由' : '已停用'}</dd></div></dl>${data.active ? `<p class="metadata">特徵規則 <code>${h(registered[data.active].feature)}</code>${registered[data.active].advice ? ` · 建議規則 <code>${h(registered[data.active].advice)}</code>` : ' · 此版本不新增建議'}</p>` : ''}</div></section><section class="panel observation-policy-controls" aria-labelledby="observation-policy-change-title"><div class="panel-header"><h2 id="observation-policy-change-title">明示切換或回退</h2></div><div class="panel-body"><label for="observation-policy-version">已註冊的觀測版本</label><div class="observation-policy-actions"><select id="observation-policy-version" ${current ? '' : 'disabled'}><option value="">選擇已註冊版本</option>${choices.map(version => `<option value="${h(version)}" ${selected === version ? 'selected' : ''}>${h(registered[version].label)} · ${h(version)}</option>`).join('')}</select><button id="observation-policy-activate" class="button primary" type="button" ${current && selected && !(data.source === 'operator' && selected === data.active) ? '' : 'disabled'}>確認切換版本</button><button id="observation-policy-disable" class="button" type="button" ${current && (data.source !== 'operator' || data.active !== null) ? '' : 'disabled'}>停用新任務擷取</button></div><p class="metadata">回退時選擇原版本並確認；既有特徵、建議、帳務及保存期限仍保留。切換不發送模型請求。</p></div></section>`}<p class="metadata"><a href="#tasks">查看任務當時的特徵與建議 →</a></p></div>`;
    const select = root.querySelector('#observation-policy-version');
    if (select) select.onchange = event => {selected = event.target.value;render();};
    root.querySelector('#observation-policy-activate')?.addEventListener('click', () => propose(selected));
    root.querySelector('#observation-policy-disable')?.addEventListener('click', () => propose(null));
    if (focus) (document.getElementById(focus) || root).focus({preventScroll:true});
  }
  function propose(version) {
    if (!data || readState !== 'current' || applying || (version !== null && !data.versions.includes(version))) return;
    const original = data, currentGeneration = generation, actionContext = context;
    const before = activeLabel(), after = versionLabel(version);
    actionContext.confirmAction(version === null ? '停用新任務的觀測擷取' : '切換新任務的觀測規則',
      `<p>目前：<strong>${h(before)}</strong></p><p>確認後：<strong>${h(after)}</strong>${version ? ` <code>${h(version)}</code>` : ''}</p><p>只影響之後接受的新任務。既有任務的特徵、建議及版本保留，不重算、不補算；觀察性建議尚未採用，不改路由、模型或費用。</p>`,
      async () => {
        if (generation !== currentGeneration || context !== actionContext) throw Error('觀測規則畫面已變更，請重新讀取後再確認。');
        applying = true;notice = '';requestVersion += 1;render();
        try {
          const result = await actionContext.api('/monitor/observation-policy/activate',{revision:original.revision,version});
          if (generation !== currentGeneration || context !== actionContext) return;
          const next = projection(result);
          if (!next || next.source !== 'operator' || next.active !== version || next.revision <= original.revision) {
            readState = 'stale';throw Error(failures.observation_policy_unavailable);
          }
          data = next;readState = 'current';selected = version || '';
        } catch (error) {
          if (generation !== currentGeneration || context !== actionContext) return;
          readState = data ? 'stale' : 'unavailable';
          notice = Object.hasOwn(failures,error.code) ? failures[error.code] : failures.observation_policy_unavailable;
          throw Error(notice);
        } finally {
          if (generation === currentGeneration && context === actionContext) {applying = false;render();}
        }
      }, '新任務觀測規則已保存；既有任務保留原版本。', false);
  }
  function refresh(force = false) {
    if (!context || !mounted() || applying) return Promise.resolve();
    if (flight && !force) return flight;
    const currentGeneration = generation, currentRequest = ++requestVersion;
    const current = () => generation === currentGeneration && requestVersion === currentRequest && mounted();
    const pending = (async () => {
      try {
        const value = await context.api('/monitor/observation-policy');
        if (!current()) return;
        const next = projection(value);
        if (!next || (data && next.revision < data.revision)) throw Error('policy_projection_unconfirmed');
        const changed = JSON.stringify(data) !== JSON.stringify(next) || readState !== 'current' || notice;
        data = next;readState = 'current';notice = '';
        if (changed) render();
      } catch {
        if (!current()) return;
        readState = data ? 'stale' : 'unavailable';render();
      } finally {if (flight === pending) flight = null;}
    })();
    flight = pending;return pending;
  }
  function mount(node, nextContext) {
    if (context?.sessionVersion === nextContext.sessionVersion && root === node && mounted()) return;
    generation += 1;requestVersion += 1;root = node;context = nextContext;
    data = null;flight = null;readState = 'idle';selected = '';applying = false;notice = '';
    root.innerHTML = '<div id="observation-policy-page"></div>';render();void refresh();
  }
  function resetView() {
    generation += 1;requestVersion += 1;root = context = data = flight = null;
    readState = 'idle';selected = '';applying = false;notice = '';
  }
  setInterval(() => {void refresh();},5000);
  return {mount,refresh,resetView};
})();
