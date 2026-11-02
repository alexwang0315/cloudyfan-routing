/* 模擬試問：題目與結果只留在這個頁面的記憶體，頁面內容一律標示為合成資料。 */
'use strict';
window.CloudyFanPlayground = (() => {
  let root, context, generation = 0, descriptor = null, readError = '', sending = false, prompt = '', scenario = 'high_pass', outcome = null, sendError = '';
  const h = value => context.esc(value);
  const modes = {cloudyfan: 'CloudyFan MRC', fixed_high: '全大模型優先', low_first: '全便宜模型優先', open_source_first: '全開源模型優先'};
  const tiers = {open_source: '開源池', low: '低分池', mid: '中分池', high: '高分池'};
  const results = {passed: ['公開驗收通過', 'green'], failed: ['公開驗收未通過', 'red'], unverifiable: ['公開驗收無法確認', 'amber']};
  const stages = {low_mid: '低分池／中分池二選一', mid_high: '中分池／高分池二選一'};
  const failures = {invalid_text_prompt: '請輸入題目，長度不可超過 32 KB。', service_busy: '目前有另一件任務正在執行，請稍後再送。', playground_not_available: '這個環境沒有提供模擬試問。'};
  const tierName = value => Object.hasOwn(tiers, value) ? tiers[value] : '未知模型池';
  const usd = value => typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value) ? '$' + value : '未知';

  function mount(node, nextContext) {
    const sameSession = context?.sessionVersion === nextContext.sessionVersion;
    context = nextContext;
    if (sameSession && root === node && root.querySelector('#playground-page')) return;
    root = node; readError = '';
    if (!sameSession) { generation += 1; descriptor = null; prompt = ''; outcome = null; sendError = ''; sending = false; }
    root.innerHTML = '<div id="playground-page"></div>';
    render();
    if (!descriptor) read();
  }
  function resetView() { generation += 1; root = null; context = null; descriptor = null; readError = ''; sending = false; prompt = ''; outcome = null; sendError = ''; }
  async function read() {
    const mine = generation;
    try { const value = await context.api('/monitor/playground'); if (mine !== generation) return; descriptor = value; }
    catch (failure) { if (mine !== generation || failure.stale) return; readError = failures[failure.code] || failure.message; }
    render();
  }
  async function send() {
    if (sending) return;
    const mine = generation;
    sending = true; sendError = ''; outcome = null; render();
    try { const value = await context.api('/monitor/playground/ask', {prompt, scenario}); if (mine !== generation) return; outcome = value; }
    catch (failure) { if (mine !== generation || failure.stale) return; sendError = failures[failure.code] || failure.message; }
    sending = false; render();
    if (outcome) root.querySelector('#playground-result')?.focus();
  }

  function steps(task) {
    const path = Array.isArray(task.path) ? task.path : [];
    const decisions = Array.isArray(task.trace?.decisions) ? task.trace.decisions : [];
    return path.map((step, index) => {
      const decision = decisions[index - 1];
      const jev = index > 0 && decision ? `<li class="playground-step jev"><span class="playground-marker">J</span><div><h3>Jev 選下一個模型池</h3><p>${h(stages[decision.stage] || '選池階段未知')}：選 <b>${h(tierName(decision.selected_tier))}</b>${decision.fallback === true ? '（分類無法使用，改走保守備援）' : ''}</p><p class="metadata">分類費用 ${h(usd(decision.cost_usd))}</p></div></li>` : '';
      const state = step.passed === true ? ['通過', 'green'] : step.passed === false ? ['未通過', 'red'] : ['未知', 'amber'];
      const models = Array.isArray(step.models) && step.models.length ? step.models.join('、') : '未知';
      return `${jev}<li class="playground-step"><span class="playground-marker">${index + 1}</span><div><h3>第 ${index + 1} 次作答：${h(tierName(step.tier))} <span class="badge ${state[1]}">公開驗收${h(state[0])}</span></h3><p class="metadata mono">${h(models)}</p></div></li>`;
    }).join('');
  }
  function resultSection() {
    if (sending) return '<section class="panel playground-result" role="status">正在走過 MRC 流程…</section>';
    if (sendError) return `<section class="panel playground-result playground-failure" role="alert">${h(sendError)}</section>`;
    if (!outcome) return '';
    const state = Object.hasOwn(results, outcome.status) ? results[outcome.status] : ['任務未完成', 'amber'];
    const costs = outcome.costs || {};
    return `<section class="panel playground-result" id="playground-result" tabindex="-1" aria-label="試問結果">
      <div class="playground-result-heading"><div><h2>MRC 的處理過程</h2><p class="metadata mono">${h(outcome.run_id)}</p></div><span class="badge ${state[1]}">${h(state[0])}</span></div>
      <ol class="playground-steps">${steps(outcome.task || {})}</ol>
      <dl class="playground-facts"><div><dt>完整 API 費用（合成）</dt><dd>${h(usd(costs.complete_api_usd))}</dd></div><div><dt>流程耗時（模擬）</dt><dd>${typeof outcome.wall_seconds === 'number' ? h(outcome.wall_seconds.toFixed(2)) + ' 秒' : '未知'}</dd></div><div><dt>模擬答案</dt><dd>${h(outcome.answer ?? '未知')}</dd></div></dl>
      <p class="playground-answer-note">模擬上游不讀題目內容，答案固定是算術題的 2 或 3，並不是在回答你的問題。</p>
      <details class="playground-detail"><summary>查看完整呼叫與選池明細</summary>${window.CloudyFanTrace?.render(outcome.task || {}) || '<p>明細尚未載入。</p>'}</details>
      <p class="metadata">這筆紀錄也會出現在 <a href="#tasks">任務與升級</a>。</p>
    </section>`;
  }
  function render() {
    const page = root?.querySelector('#playground-page');
    if (!page) return;
    const intro = '<div class="page-heading"><div><div class="eyebrow">MRC / SIMULATION</div><h1>試問 MRC（模擬）</h1><p>送出一題，看它在 MRC 內怎麼走：先給哪個模型池、驗收結果、Jev 怎麼選、花多少錢。</p></div></div>';
    if (readError) { page.innerHTML = `${intro}<div class="panel playground-result playground-failure" role="alert">${h(readError)}</div>`; return; }
    if (!descriptor) { page.innerHTML = `${intro}<div class="panel empty" role="status">正在讀取試問設定…</div>`; return; }
    page.innerHTML = `${intro}<div class="playground-warning" role="note"><b>這是模擬，不是真實模型。</b> ${h(descriptor.notice)}。流程程式是真的 MRC，走哪條路由則由下方情境決定。</div>
      <form class="panel playground-form" id="playground-form">
        <label for="playground-prompt">題目</label>
        <textarea id="playground-prompt" rows="4" maxlength="32768" required placeholder="輸入你想試的問題"></textarea>
        <label for="playground-scenario">模擬情境</label>
        <select id="playground-scenario">${descriptor.scenarios.map(item => `<option value="${h(item.id)}" ${item.id === scenario ? 'selected' : ''}>${h(item.label)}</option>`).join('')}</select>
        <div class="playground-actions"><button class="button primary" type="submit" ${sending ? 'disabled' : ''}>${sending ? '執行中…' : '送出模擬'}</button><span class="metadata">目前模式：${h(modes[descriptor.effective_mode] || '未知')}；以 ${h(descriptor.client)} 客戶端身分送出，題目會存在本機模擬資料夾</span></div>
      </form>${resultSection()}`;
    page.querySelector('#playground-prompt').value = prompt;
    page.querySelector('#playground-prompt').oninput = event => { prompt = event.target.value; };
    page.querySelector('#playground-scenario').onchange = event => { scenario = event.target.value; };
    page.querySelector('#playground-form').onsubmit = event => { event.preventDefault(); send(); };
  }
  return {mount, resetView};
})();
