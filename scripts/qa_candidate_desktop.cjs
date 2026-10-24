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
  assert.ok(core, 'Use the prepared immutable Core root');
  const playwrightPath = process.env.PLAYWRIGHT_PATH || '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright';
  const { chromium } = require(playwrightPath);
  const output = process.argv[2] ? path.resolve(process.argv[2]) : path.join(root, 'runtime', 'candidate-desktop', randomUUID().slice(0, 12));
  await fs.mkdir(path.dirname(output), { recursive: true });
  await fs.mkdir(output, { recursive: false });
  const report = { schema_version: 1, viewport: { width: 1280, height: 800 }, checks: [], screenshots: [],
    page_errors: [], console_errors: [], blocked_browser_requests: [], status: 'incomplete' };
  const fixture = startFixture(path.join(core, '.venv/bin/python'),
    [path.join(__dirname, 'qa_candidate_http.py'), '--output', output], {
      cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1',
        PYTHONPATH: [path.join(root, 'src'), path.join(core, 'src')].join(path.delimiter) },
    });
  let browser, page;
  try {
    const inputs = ['src/cloudyfan_monitor/candidate_observation.py', 'src/cloudyfan_monitor/static/candidates.js',
      'src/cloudyfan_monitor/static/app.js', 'scripts/qa_candidate_http.py', 'scripts/qa_candidate_desktop.cjs'];
    report.source_sha256 = Object.fromEntries(await Promise.all(inputs.map(async name => {
      try { return [name, createHash('sha256').update(await fs.readFile(path.join(root, name))).digest('hex')]; }
      catch (error) { if (error.code === 'ENOENT') return [name, null]; throw error; }
    })));
    const ready = await fixture.next();
    assert.equal(ready.type, 'ready');
    report.scope = 'Synthetic CategoryCore and isolated identities; actual desktop controls and loopback HTTP; mocked upstream';
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
    await page.locator('a[data-page="pools"]').click();
    await page.locator('#candidate-version').waitFor();
    await page.locator('#candidate-mode').selectOption('cloudyfan');
    const evidence = page.locator('[data-candidate-evidence="open_source:0"]');
    await evidence.getByText('三維證據已記錄', { exact: true }).waitFor();
    assert.match(await evidence.innerText(), /合成示範/);
    assert.match(await evidence.innerText(), /正式資格未知/);
    assert.match(await evidence.innerText(), /配額未知/);
    await evidence.locator('summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await evidence.locator('details').getAttribute('open'), '');
    assert.match(await evidence.innerText(), /arithmetic-small/);
    assert.match(await evidence.innerText(), /完整成本估計/);
    await evidence.locator('summary').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, '01a-candidate-viewport.png') });
    report.screenshots.push('01a-candidate-viewport.png');
    await evidence.locator('.candidate-reasons').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, '01b-reasons-viewport.png') });
    report.screenshots.push('01b-reasons-viewport.png');
    const excluded = page.locator('.candidate-excluded');
    await excluded.locator('summary').focus();
    await page.keyboard.press('Enter');
    assert.match(await excluded.innerText(), /quality_evidence_insufficient/);
    assert.equal(await excluded.locator('.model-name').count(), 0);
    await page.screenshot({ path: path.join(output, '01-registered-evidence.png'), fullPage: true });
    report.screenshots.push('01-registered-evidence.png');
    report.checks.push({ behavior: 'registered_candidate_evidence_is_separate_from_formal_qualification', passed: true });

    await page.locator('#candidate-mode').selectOption('fixed_high');
    await page.locator('#candidate-context').getByText('契約未登錄', { exact: true }).waitFor();
    await page.locator('#candidate-version').selectOption(ready.pools[1]);
    await page.locator('#candidate-context').getByText('未啟用版本', { exact: true }).waitFor();
    assert.match(await page.locator('[data-candidate-evidence="open_source:0"]').innerText(), /資格證據未提供/);
    report.checks.push({ behavior: 'version_and_mode_selection_do_not_borrow_registration_or_evidence', passed: true });
    await page.screenshot({ path: path.join(output, '02-inactive-proposal.png'), fullPage: true });
    report.screenshots.push('02-inactive-proposal.png');

    await page.locator('#candidate-version').selectOption(ready.pools[0]);
    await page.locator('#candidate-mode').selectOption('cloudyfan');
    await page.locator('#category').selectOption('design-arena:models:website');
    await page.waitForFunction(() => document.querySelector('#candidate-context')?.textContent.includes('design-arena:models:website'));
    await page.locator('#candidate-context').getByText('契約未登錄', { exact: true }).waitFor();
    assert.equal(await page.locator('.pool-candidate .model-name').count(), 0);
    await page.locator('#category').selectOption(ready.category);
    await evidence.getByText('三維證據已記錄', { exact: true }).waitFor();
    assert.match(await page.locator('[data-candidate-evidence="low:1"]').innerText(), /設定身分缺項/);
    report.checks.push({ behavior: 'categories_missing_identity_and_empty_positions_remain_distinct', passed: true });

    fixture.send({ command: 'inspect' });
    const before = await fixture.next();
    const request = await context.request.post(ready.origin + '/v1/tasks', { headers: { Authorization: 'Bearer ' + ready.credentials.studio },
      data: { run_id: 'candidate-observation-original', category: ready.category, prompt: 'Return only 1+1.' } });
    assert.equal(request.status(), 200);
    fixture.send({ command: 'inspect' });
    const sent = await fixture.next();
    assert.equal(sent.mock_upstream_call_count, before.mock_upstream_call_count + 1);
    for (let repeat = 0; repeat < 2; repeat++) {
      const read = page.waitForResponse(response => response.url().includes('/monitor/pools') && response.status() === 200);
      await page.locator('#reload').click();
      await (await read).finished();
    }
    const own = await context.request.get(ready.origin + '/v1/monitor', { headers: { Authorization: 'Bearer ' + ready.credentials.studio } });
    assert.equal(own.status(), 200);
    assert.ok(!JSON.stringify(await own.json()).includes('mrc-observation-v1'));
    fixture.send({ command: 'inspect' });
    const after = await fixture.next();
    assert.equal(after.mock_upstream_call_count, sent.mock_upstream_call_count);
    assert.equal(after.known_api_usd, sent.known_api_usd);
    assert.equal(after.reserved_api_usd, sent.reserved_api_usd);
    assert.deepEqual(after.control, sent.control);
    assert.deepEqual(after.blocked_server_socket_attempts, []);
    report.checks.push({ behavior: 'observation_is_operator_only_and_adds_no_dispatch_or_fees',
      model_calls_before_observation: sent.mock_upstream_call_count, model_calls_after_observation: after.mock_upstream_call_count, passed: true });
    assert.deepEqual(report.page_errors, []);
    assert.deepEqual(report.console_errors, []);
    assert.deepEqual(report.blocked_browser_requests, []);
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
    await fixture.stop();
    await fs.writeFile(path.join(output, 'desktop-receipt.json'), JSON.stringify(report, null, 2));
    process.stdout.write(JSON.stringify({ status: report.status, output, checks: report.checks.length }) + '\n');
  }
}
main().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
