#!/usr/bin/env node
'use strict';
// Existing local Playwright + Chrome, actual 1280x800 desktop; no downloads or mobile contexts.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const { startFixture } = require('./qa_fixture_ipc.cjs');

async function main() {
  const root = path.resolve(__dirname, '..');
  const core = process.env.CLOUDYFAN_CORE_ROOT || '/Users/alexwang0315/Documents/CloudyFan/cloudyfan-jev-core';
  const playwrightPath = process.env.PLAYWRIGHT_PATH || '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright';
  const { chromium } = require(playwrightPath);
  const parent = path.join(root, 'runtime', 'mode-desktop');
  await fs.mkdir(parent, { recursive: true });
  const output = process.argv[2] ? path.resolve(process.argv[2]) : path.join(parent, randomUUID().slice(0, 12));
  await fs.mkdir(output);
  const report = { schema_version: 1, viewport: { width: 1280, height: 800 }, checks: [], screenshots: [],
    page_errors: [], console_errors: [], blocked_browser_requests: [], api_responses: [], status: 'incomplete' };
  const fixture = startFixture(path.join(core, '.venv', 'bin', 'python'),
    [path.join(__dirname, 'qa_mode_http.py'), '--output', output], {
      cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1',
        PYTHONPATH: [path.join(root, 'src'), path.join(core, 'src')].join(path.delimiter) },
    });
  let browser;
  const sourcePaths = ['tests/test_http_mode_acceptance.py', 'tests/test_category_host_integration.py',
    'scripts/qa_mode_desktop.cjs', 'scripts/qa_mode_http.py', 'scripts/qa_fixture_ipc.cjs',
    'src/cloudyfan_monitor/static/app.js'];
  try {
    report.source_sha256 = Object.fromEntries(await Promise.all(sourcePaths.map(async name =>
      [name, createHash('sha256').update(await fs.readFile(path.join(root, name))).digest('hex')])));
    const ready = await fixture.next();
    assert.equal(ready.type, 'ready');
    report.origin = ready.origin;
    report.scope = 'synthetic CategoryCore and baseline Core runners; actual desktop controls and loopback HTTP; mocked upstream';
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    report.browser = browser.version();
    const context = await browser.newContext({ viewport: report.viewport, serviceWorkers: 'block' });
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin === ready.origin) return route.continue();
      report.blocked_browser_requests.push({ origin: url.origin, method: route.request().method() });
      return route.abort();
    });
    const page = await context.newPage();
    page.on('pageerror', error => report.page_errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') report.console_errors.push(message.text()); });
    await page.goto(ready.origin);
    await page.locator('#mode-global').waitFor();
    assert.equal(await page.locator('#mode-global option:disabled').count(), 0);

    async function api(principal, method, route, body) {
      const target = new URL(route, ready.origin);
      assert.equal(target.origin, ready.origin);
      const credential = principal === 'operator' ? ready.operator : ready.credentials[principal];
      const response = await context.request.fetch(target.href, { method,
        headers: { Authorization: 'Bearer ' + credential }, ...(body === undefined ? {} : { data: body }) });
      report.api_responses.push({ principal, method, path: route, status: response.status() });
      const value = await response.json();
      assert.equal(response.status(), 200, JSON.stringify(value));
      return value;
    }
    async function changeMode(mode, client = '') {
      await page.locator(`#mode-${client || 'global'}`).selectOption(mode);
      await page.locator(`[data-change-mode="${client}"]`).click();
      await page.locator('#confirm[open]').waitFor();
      const changed = page.waitForResponse(response => response.url().endsWith('/monitor/mode')
        && response.request().method() === 'POST');
      await page.locator('#apply-confirm').click();
      const response = await changed;
      assert.equal(response.status(), 200, JSON.stringify(await response.json()));
      await page.locator('#confirm').waitFor({ state: 'hidden' });
      await page.waitForFunction(() => document.querySelector('#apply-confirm').disabled === false);
      await page.locator(`#mode-${client || 'global'}`).waitFor();
      assert.equal(await page.locator(`#mode-${client || 'global'}`).inputValue(), mode);
    }
    async function submit(principal, run, model) {
      const created = await api(principal, 'POST', '/v1/tasks',
        { run_id: run, category: ready.category, prompt: 'Return only 1+1.' });
      assert.equal(created.status, 'passed');
      const outcome = await api(principal, 'GET', `/v1/tasks/${run}/result`);
      assert.equal(outcome.answer, '2');
      assert.equal(outcome.provenance.attempts[0].calls[0].actual_model, model);
      return outcome;
    }
    async function screenshot(name, locator = page) {
      const file = name + '.png';
      await locator.screenshot({ path: path.join(output, file), ...(locator === page ? { fullPage: true } : {}) });
      report.screenshots.push(file);
    }
    async function reload() {
      const loaded = page.waitForResponse(response => response.url().endsWith('/monitor/snapshot') && response.status() === 200);
      await page.locator('#reload').click();
      await (await loaded).finished();
      await page.waitForFunction(() => document.querySelector('#error').hidden === true);
    }
    const modes = [['cloudyfan', 'example/open_source'], ['fixed_high', 'example/high'],
      ['low_first', 'example/low'], ['open_source_first', 'example/open_source']];
    let original;
    for (const [mode, model] of modes) {
      await changeMode(mode);
      const result = await submit('studio', 'desktop-' + mode, model);
      if (mode === 'cloudyfan') {
        original = result;
        assert.equal(result.provenance.routing_version, 'hierarchical-qcs-v1');
      } else assert.equal(result.provenance.routing_version, 'hierarchical-sequential-v1');
      report.checks.push({ behavior: 'global_mode_dispatch', mode, first_model: model, passed: true });
    }
    await reload();
    await screenshot('01-four-modes');

    await page.locator('a[data-page="clients"]').click();
    await page.locator('#mode-studio').waitFor();
    await changeMode('low_first', 'studio');
    await submit('studio', 'desktop-override', 'example/low');
    await submit('agent', 'desktop-inherit', 'example/open_source');
    await reload();
    let snapshot = await api('operator', 'GET', '/monitor/snapshot');
    assert.equal(snapshot.clients.find(client => client.id === 'studio').effective_mode, 'low_first');
    assert.equal(snapshot.clients.find(client => client.id === 'agent').effective_mode, 'open_source_first');
    assert.equal(snapshot.ledger.known_usd, '0.06');
    assert.equal(snapshot.ledger.accounts.length, 1);
    report.checks.push({ behavior: 'client_override_and_global_inheritance', passed: true });
    await screenshot('02-client-override');

    await page.locator('a[data-page="pools"]').click();
    await page.locator('#category').waitFor();
    assert.equal(await page.locator('#category option').count(), 23);
    const matrix = page.locator('section.panel').filter({ has: page.getByRole('heading', { name: '類別 × 四模式契約', exact: true }) });
    assert.equal(await matrix.locator('tbody tr').count(), 23);
    assert.equal(await matrix.getByText('契約已註冊', { exact: true }).count(), 4);
    assert.equal(await matrix.getByText('未支援', { exact: true }).count(), 88);
    assert.equal(await matrix.getByText('缺少此類別與模式的契約', { exact: true }).count(), 88);
    assert.equal(await page.locator('.pool-candidate .model-name').count(), 0);
    report.checks.push({ behavior: '23_by_4_contract_matrix', registered: 4, missing: 88,
      candidate_positions_named: 0, passed: true });
    await screenshot('03-contract-matrix', matrix);

    // Publish this already registered synthetic second view to display its existing activate control.
    await api('operator', 'POST', '/monitor/pools/publish', { pool: ready.pools[1], revision: snapshot.control.revision });
    await reload();
    // Snapshot and candidate data load separately; wait for the visible new
    // candidate version before the control can capture its current revision.
    await page.locator('.notice').filter({ hasText: '候選 ' + ready.pools[1].slice(0, 12) }).waitFor();
    await page.locator('#activate-pool:enabled').waitFor();
    await page.locator('#activate-pool').click();
    await page.locator('#confirm[open]').waitFor();
    const activated = page.waitForResponse(response => response.url().endsWith('/monitor/pools/activate')
      && response.request().method() === 'POST');
    await page.locator('#apply-confirm').click();
    const activation = await activated;
    assert.equal(activation.status(), 200, JSON.stringify(await activation.json()));
    await page.locator('#confirm').waitFor({ state: 'hidden' });
    await page.waitForFunction(() => document.querySelector('#apply-confirm').disabled === false);
    await submit('studio', 'desktop-after-pool', 'example/low');
    snapshot = await api('operator', 'GET', '/monitor/snapshot');
    assert.equal(snapshot.control.routing_pool, ready.pools[1]);
    assert.equal(snapshot.tasks.find(task => task.run === 'desktop-cloudyfan').pool, ready.pools[0]);
    assert.equal(snapshot.tasks.find(task => task.run === 'desktop-after-pool').pool, ready.pools[1]);
    report.checks.push({ behavior: 'pool_switch_only_changes_future_tasks', passed: true });

    fixture.send({ command: 'reopen' });
    const reopened = await fixture.next();
    assert.equal(reopened.type, 'reopened');
    assert.equal(reopened.mock_call_count, 7);
    const retained = await api('studio', 'GET', '/v1/tasks/desktop-cloudyfan/result');
    assert.deepEqual(retained, original);
    assert.equal((await api('studio', 'POST', '/v1/tasks', { run_id: 'desktop-cloudyfan',
      category: ready.category, prompt: 'Return only 1+1.' })).status, 'passed');
    assert.equal((await api('studio', 'POST', '/v1/tasks/desktop-cloudyfan/resume', {})).status, 'passed');
    await page.locator('a[data-page="tasks"]').click();
    await reload();
    assert.equal(await page.locator('#task-search').count(), 1);
    await page.locator('#task-search').fill('desktop-cloudyfan');
    await page.waitForFunction(() => document.querySelector('#task-search').value === 'desktop-cloudyfan');
    const row = page.locator('#main tbody tr').filter({ hasText: 'desktop-cloudyfan' });
    assert.equal(await row.count(), 1);
    assert.match(await row.innerText(), /CloudyFan MRC/);
    await screenshot('04-original-task-after-reopen');
    snapshot = await api('operator', 'GET', '/monitor/snapshot');
    assert.equal(snapshot.summary.passed, 7);
    assert.equal(snapshot.ledger.known_usd, '0.07');
    assert.equal(snapshot.ledger.accounts.length, 1);
    const httpReceipt = JSON.parse(await fs.readFile(path.join(output, 'http-receipt.json'), 'utf8'));
    assert.equal(httpReceipt.mock_upstream_call_count, 7);
    assert.equal(httpReceipt.blocked_server_socket_attempts.length, 0);
    assert.ok(httpReceipt.http_responses.length > 0);
    assert.ok(httpReceipt.http_responses.every(response => response.remote_addr === '127.0.0.1'));
    report.checks.push({ behavior: 'reopen_preserves_original_contract_and_shared_ledger', model_calls: 7,
      passed_tasks: 7, known_api_usd: '0.07', passed: true });
    assert.equal(report.page_errors.length, 0);
    assert.equal(report.console_errors.length, 0);
    assert.equal(report.blocked_browser_requests.length, 0);
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.failure = { name: error.name, message: error.message, stack: error.stack };
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {
      report.browser_cleanup_error = 'Browser cleanup failed'; report.status = 'failed'; process.exitCode = 1;
    });
    const outcome = await fixture.stop();
    report.fixture_exit = outcome;
    if (outcome.timed_out || outcome.cleanup_failed || outcome.code !== 0) { report.status = 'failed'; process.exitCode = 1; }
    if (outcome.stderr_present) report.fixture_error = 'Fixture stderr present; inspect locally';
    await fs.writeFile(path.join(output, 'desktop-report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ status: report.status, checks: report.checks.length, output }));
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
