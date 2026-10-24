#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const { startFixture } = require('./qa_fixture_ipc.cjs');

async function main() {
  const root = path.resolve(__dirname, '..');
  const core = process.env.CLOUDYFAN_CORE_ROOT;
  assert.ok(core, 'Use an explicitly matched Core root');
  const { chromium } = require(process.env.PLAYWRIGHT_PATH || '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
  const output = process.argv[2] ? path.resolve(process.argv[2]) : path.join(root, 'runtime', 'capacity-sources-desktop', randomUUID().slice(0, 12));
  await fs.mkdir(path.dirname(output), { recursive: true });
  await fs.mkdir(output, { recursive: false });
  const report = { schema_version: 1, viewport: { width: 1280, height: 800 }, checks: [], screenshots: [],
    page_errors: [], console_errors: [], blocked_browser_requests: [], status: 'incomplete',
    scope: 'synthetic controlled host; actual desktop and loopback HTTP; mocked supplier/model traffic; no real supplier qualification' };
  const fixture = startFixture(process.env.CLOUDYFAN_QA_PYTHON || path.join(core, '.venv/bin/python'),
    [path.join(__dirname, 'qa_capacity_sources_http.py'), '--output', output], {
      cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1',
        PYTHONPATH: [path.join(root, 'src'), path.join(core, 'src')].join(path.delimiter) },
    });
  let browser, page;
  const inspect = async command => { fixture.send(command || { command: 'inspect' }); return fixture.next(); };
  try {
    const inputs = ['src/cloudyfan_monitor/capacity.py', 'src/cloudyfan_monitor/static/capacity.js',
      'src/cloudyfan_monitor/static/app.js', 'scripts/qa_capacity_sources_http.py', 'scripts/qa_capacity_sources_desktop.cjs'];
    report.source_sha256 = Object.fromEntries(await Promise.all(inputs.map(async name => {
      try { return [name, createHash('sha256').update(await fs.readFile(path.join(root, name))).digest('hex')]; }
      catch (error) { if (error.code === 'ENOENT') return [name, null]; throw error; }
    })));
    const ready = await fixture.next();
    assert.equal(ready.type, 'ready');
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    report.browser = browser.version();
    const context = await browser.newContext({ viewport: report.viewport, serviceWorkers: 'block' });
    await context.route('**/*', async route => {
      if (new URL(route.request().url()).origin === ready.origin) return route.continue();
      report.blocked_browser_requests.push({ method: route.request().method(), origin: new URL(route.request().url()).origin });
      return route.abort();
    });
    page = await context.newPage();
    page.setDefaultTimeout(6000);
    page.on('pageerror', error => report.page_errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') report.console_errors.push(message.text()); });
    await page.goto(ready.origin);
    await page.locator('a[data-page="capacity"]').click();
    await page.locator('#capacity-page').waitFor();
    const shared = page.locator('[data-capacity-group="account-shared"]');
    await shared.locator('summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await shared.getAttribute('open'), '');
    assert.match(await shared.innerText(), /RPM/);
    assert.match(await shared.innerText(), /TPM/);
    assert.match(await shared.innerText(), /30000/);
    assert.match(await shared.innerText(), /供應商限制/);
    assert.match(await shared.innerText(), /本機保守上限/);
    assert.match(await page.locator('#capacity-bindings').innerText(), /binding-a/);
    assert.match(await page.locator('#capacity-bindings').innerText(), /binding-b/);
    const contrast = await shared.locator('.capacity-limit-title strong').first().evaluate(node => {
      const rgb = text => text.match(/[\d.]+/g).slice(0, 3).map(Number);
      const luminance = values => values.map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
        .reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
      const foreground = luminance(rgb(getComputedStyle(node).color));
      const background = luminance(rgb(getComputedStyle(node.closest('.capacity-limit')).backgroundColor));
      return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
    });
    assert.ok(contrast >= 4.5, 'Capacity fields must remain readable in the existing dark theme');
    await page.screenshot({ path: path.join(output, '01-shared-units.png'), fullPage: true });
    report.screenshots.push('01-shared-units.png');
    report.checks.push({ behavior: 'operator_can_expand_shared_scopes_and_distinct_units', passed: true });

    const before = await inspect();
    const read = page.waitForResponse(response => response.url().endsWith('/monitor/capacity') && response.request().method() === 'GET');
    await page.locator('#reload').click();
    await (await read).finished();
    await shared.locator('summary').focus();
    const poll = page.waitForResponse(response => response.url().endsWith('/monitor/capacity') && response.request().method() === 'GET');
    await (await poll).finished();
    await page.waitForFunction(() => document.activeElement?.closest('[data-capacity-group]')?.dataset.capacityGroup === 'account-shared');
    assert.equal(await shared.getAttribute('open'), '');
    const after = await inspect();
    assert.equal(after.capacity_sha256, before.capacity_sha256);
    assert.equal(after.health_sha256, before.health_sha256);
    assert.equal(after.mock_source_requests.length, 0);
    assert.equal(after.mock_model_request_count, 0);
    assert.deepEqual(after.control, before.control);
    report.checks.push({ behavior: 'polling_preserves_disclosure_focus_and_saved_capacity_health_bytes', passed: true });

    async function refreshSources() {
      await page.locator('#capacity-refresh-sources').click();
      await page.locator('#confirm[open]').waitFor();
      const response = page.waitForResponse(value => value.url().endsWith('/monitor/capacity/refresh'));
      await page.locator('#apply-confirm').click();
      const result = await response;
      assert.equal(result.status(), 200);
      await page.locator('#confirm[open]').waitFor({ state: 'hidden' });
      return result.json();
    }
    const refreshed = await refreshSources();
    assert.equal(refreshed.request_count, 2);
    const key = page.locator('[data-capacity-group="key-a"]');
    await key.locator('summary').focus();
    await page.keyboard.press('Enter');
    await key.locator('[data-capacity-dimension="supplier:usd"]').getByText('20', { exact: true }).waitFor();
    assert.match(await key.innerText(), /daily|每日/);
    assert.match(await key.locator('[data-capacity-dimension="supplier:rpm"]').innerText(), /未知/);
    await page.screenshot({ path: path.join(output, '02-explicit-key-limit.png'), fullPage: true });
    report.screenshots.push('02-explicit-key-limit.png');
    report.checks.push({ behavior: 'explicit_refresh_reads_two_keys_without_credits_or_model_calls', passed: true });

    await inspect({ command: 'fail_a' });
    const partial = await refreshSources();
    assert.equal(partial.status, 'partial');
    await key.getByText('保留上次可信資料', { exact: false }).waitFor();
    assert.match(await key.innerText(), /來源拒絕存取/);
    const partialState = await inspect();
    const again = page.waitForResponse(response => response.url().endsWith('/monitor/capacity') && response.request().method() === 'GET');
    await page.locator('#reload').click();
    await (await again).finished();
    const readOnly = await inspect();
    assert.equal(readOnly.mock_source_requests.length, 4);
    assert.equal(readOnly.capacity_sha256, partialState.capacity_sha256);
    assert.equal(readOnly.mock_model_request_count, 0);
    assert.ok(readOnly.mock_source_requests.every(request => request.method === 'GET' && request.path === '/api/v1/key'));
    assert.ok(!(await page.locator('#capacity-page').innerText()).includes('PRIVATE_'));
    await page.screenshot({ path: path.join(output, '03-partial-failure.png'), fullPage: true });
    report.screenshots.push('03-partial-failure.png');
    report.checks.push({ behavior: 'partial_failure_retains_original_times_and_safe_reasons_without_implicit_retry', passed: true });

    const task = await context.request.post(ready.origin + '/v1/tasks', { headers: { Authorization: 'Bearer ' + ready.credentials.studio },
      data: { run_id: 'capacity-original-dispatch', category: ready.category, prompt: 'Return only 1+1.' } });
    assert.equal(task.status(), 200);
    assert.equal((await task.json()).status, 'passed');
    const dispatched = await inspect();
    assert.equal(dispatched.mock_model_request_count, 1);
    assert.equal(dispatched.mock_source_requests.length, 4);
    await inspect({ command: 'advance', seconds: 301 });
    const staleRead = page.waitForResponse(response => response.url().endsWith('/monitor/capacity') && response.request().method() === 'GET');
    await page.locator('#reload').click();
    await (await staleRead).finished();
    await key.locator('[data-capacity-dimension="supplier:usd"]').getByText('已過期', { exact: true }).waitFor();
    const savedResult = await context.request.get(ready.origin + '/v1/tasks/capacity-original-dispatch/result',
      { headers: { Authorization: 'Bearer ' + ready.credentials.studio } });
    assert.equal(savedResult.status(), 200);
    const sourceExpired = await inspect();
    assert.equal(sourceExpired.known_api_usd, dispatched.known_api_usd);
    assert.equal(sourceExpired.reserved_api_usd, dispatched.reserved_api_usd);
    assert.equal(sourceExpired.mock_model_request_count, 1);
    assert.equal(sourceExpired.mock_source_requests.length, 4);
    await inspect({ command: 'advance', seconds: 30 * 86400 });
    const expired = await context.request.get(ready.origin + '/v1/tasks/capacity-original-dispatch/result',
      { headers: { Authorization: 'Bearer ' + ready.credentials.studio } });
    assert.equal(expired.status(), 410);
    const own = await context.request.get(ready.origin + '/v1/monitor', { headers: { Authorization: 'Bearer ' + ready.credentials.agent } });
    assert.equal(own.status(), 200);
    assert.ok(!JSON.stringify(await own.json()).includes('capacity-sources-v1'));
    const final = await inspect();
    assert.equal(final.mock_model_request_count, 1);
    assert.equal(final.mock_source_requests.length, 4);
    assert.deepEqual(final.blocked_server_socket_attempts, []);
    report.checks.push({ behavior: 'expired_sources_preserve_original_task_cost_result_and_30day_access_boundary', passed: true });
    assert.deepEqual(report.page_errors, []);
    assert.deepEqual(report.console_errors, []);
    assert.deepEqual(report.blocked_browser_requests, []);
    await page.locator('#logout').click();
    assert.match(await page.locator('#main').innerText(), /已清除登入/);
    assert.equal(await page.locator('[data-capacity-group]').count(), 0);
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.failure = { name: error.name, message: error.message, stack: error.stack };
    if (page) {
      await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
      report.screenshots.push('failure.png');
    }
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    report.fixture_stop = await fixture.stop();
    await fs.writeFile(path.join(output, 'desktop-receipt.json'), JSON.stringify(report, null, 2));
    process.stdout.write(JSON.stringify({ status: report.status, output, checks: report.checks.length }) + '\n');
  }
}
main().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
