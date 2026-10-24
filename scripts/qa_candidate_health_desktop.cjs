#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const { startFixture } = require('./qa_fixture_ipc.cjs');

async function main() {
  const root = path.resolve(__dirname, '..'), core = process.env.CLOUDYFAN_CORE_ROOT;
  assert.ok(core, 'Use the prepared immutable Core root');
  const playwrightPath = process.env.PLAYWRIGHT_PATH || '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright';
  const { chromium } = require(playwrightPath);
  const output = process.argv[2] ? path.resolve(process.argv[2]) : path.join(root, 'runtime', 'candidate-health-desktop', randomUUID().slice(0, 12));
  await fs.mkdir(path.dirname(output), { recursive: true });
  await fs.mkdir(output, { recursive: false });
  const report = { schema_version: 1, viewport: { width: 1280, height: 800 }, checks: [], screenshots: [],
    observations: [], page_errors: [], console_errors: [], blocked_browser_requests: [], status: 'incomplete' };
  const fixture = startFixture(path.join(core, '.venv/bin/python'),
    [path.join(__dirname, 'qa_candidate_health_http.py'), '--output', output], {
      cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1',
        PYTHONPATH: [path.join(root, 'src'), path.join(core, 'src')].join(path.delimiter) },
    });
  let browser, page;
  const command = async name => { fixture.send({ command: name }); return fixture.next(); };
  try {
    const inputs = ['src/cloudyfan_monitor/candidate_observation.py', 'src/cloudyfan_monitor/static/candidates.js',
      'src/cloudyfan_monitor/static/candidates.css', 'src/cloudyfan_monitor/static/app.js',
      'scripts/qa_candidate_health_http.py', 'scripts/qa_candidate_health_desktop.cjs', 'tests/candidate_health_fixture.py'];
    report.source_sha256 = Object.fromEntries(await Promise.all(inputs.map(async name =>
      [name, createHash('sha256').update(await fs.readFile(path.join(root, name))).digest('hex')])));
    const ready = await fixture.next();
    assert.equal(ready.type, 'ready');
    report.scope = 'Synthetic BackupCategoryCore/HealthStore; actual Chrome and loopback HTTP; mocked model upstream';
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    report.browser = browser.version();
    const context = await browser.newContext({ viewport: report.viewport, serviceWorkers: 'block' });
    await context.route('**/*', async route => {
      if (new URL(route.request().url()).origin === ready.origin) return route.continue();
      report.blocked_browser_requests.push({ method: route.request().method(), origin: new URL(route.request().url()).origin });
      return route.abort();
    });
    page = await context.newPage();
    page.setDefaultTimeout(12000);
    page.on('pageerror', error => report.page_errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') report.console_errors.push(message.text()); });
    await page.goto(ready.origin);
    await page.locator('a[data-page="pools"]').click();
    await page.locator('#candidate-version').waitFor();
    const evidence = page.locator('[data-candidate-evidence="open_source:0"]');
    const waitHealth = state => evidence.locator(`[data-candidate-health-status="${state}"]`).waitFor();
    const openEvidence = async () => {
      if (await evidence.locator('details').getAttribute('open') === null) {
        await evidence.locator('summary').focus(); await page.keyboard.press('Enter');
      }
    };
    const screenshot = async (name, fullPage = false) => {
      await page.screenshot({ path: path.join(output, name), fullPage }); report.screenshots.push(name);
    };
    await waitHealth('healthy');
    await openEvidence();
    assert.match(await evidence.innerText(), /健康條件通過/);
    assert.match(await evidence.innerText(), /正式資格未知/);
    assert.match(await evidence.innerText(), /配額未知/);
    assert.match(await evidence.innerText(), /9234/);
    assert.match(await evidence.innerText(), /端點觀測.*非整件任務/);
    await evidence.locator('.candidate-health-properties').scrollIntoViewIfNeeded();
    await screenshot('01-healthy-viewport.png');
    await screenshot('01-healthy-full.png', true);
    report.checks.push({ behavior: 'health_evidence_is_distinct_from_qualification_quota_and_authorization', passed: true });

    await command('expire_health');
    await page.locator('#crumb').click();
    await waitHealth('stale');
    assert.equal(await page.locator('#candidate-version').inputValue(), ready.pools[0]);
    assert.match(await page.locator('#candidate-context').innerText(), new RegExp(ready.pools[0]));
    await openEvidence();
    assert.match(await evidence.innerText(), /observation_stale/);
    await evidence.locator('[data-candidate-health-status]').scrollIntoViewIfNeeded();
    await screenshot('02-stale-viewport.png');
    report.checks.push({ behavior: 'same_pool_version_refreshes_health_at_original_ttl_without_manual_reload', passed: true });

    await command('publish_healthy'); await waitHealth('healthy');
    await command('availability_failures'); await waitHealth('cooldown');
    await openEvidence();
    assert.match(await evidence.innerText(), /cooldown_active/);
    await evidence.locator('[data-candidate-health-status]').scrollIntoViewIfNeeded();
    await screenshot('03-cooldown-viewport.png');
    await command('elapse_cooldown'); await page.locator('#crumb').click(); await waitHealth('recovery_required');
    const pending = await command('publish_healthy');
    await waitHealth('recovery_required');
    const poll = page.waitForResponse(response => response.url().includes('/monitor/pools') && response.status() === 200);
    await (await poll).finished();
    await waitHealth('recovery_required');
    const unchanged = await command('inspect');
    assert.equal(unchanged.health_store_sha256, pending.health_store_sha256);
    report.observations.push({ stage: 'recovery_required', before: pending.health_store_sha256, after: unchanged.health_store_sha256 });
    await openEvidence();
    assert.match(await evidence.innerText(), /recovery_required/);
    await evidence.locator('[data-candidate-health-status]').scrollIntoViewIfNeeded();
    await screenshot('04-recovery-required-viewport.png');
    await command('recover_endpoint'); await page.locator('#crumb').click(); await waitHealth('healthy');
    report.checks.push({ behavior: 'cooldown_and_recovery_required_stay_open_until_explicit_fixture_recovery', passed: true });

    await command('degrade_endpoint'); await waitHealth('degraded');
    await openEvidence();
    const visible = await evidence.innerText();
    for (const code of ['status_degraded', 'uptime_last_5m_below_minimum', 'latency_outside_limit', 'throughput_outside_limit']) assert.ok(visible.includes(code));
    await evidence.locator('.candidate-health-reasons').scrollIntoViewIfNeeded();
    await screenshot('05-multiple-reasons-viewport.png');
    await screenshot('05-multiple-reasons-full.png', true);
    report.checks.push({ behavior: 'all_original_health_exclusion_reasons_are_readable_by_keyboard', passed: true });
    await command('publish_healthy'); await page.locator('#crumb').click(); await waitHealth('healthy');

    const created = await context.request.post(ready.origin + '/v1/tasks', { headers: { Authorization: 'Bearer ' + ready.credentials.studio },
      data: { run_id: 'health-observation-original', category: ready.category, prompt: 'Return only 1+1.' } });
    assert.equal(created.status(), 200); assert.equal((await created.json()).status, 'passed');
    const before = await command('inspect');
    for (let repeat = 0; repeat < 2; repeat++) {
      const response = page.waitForResponse(value => value.url().includes('/monitor/pools') && value.status() === 200);
      await page.locator('#reload').click(); await (await response).finished();
    }
    const own = await context.request.get(ready.origin + '/v1/monitor', { headers: { Authorization: 'Bearer ' + ready.credentials.studio } });
    assert.equal(own.status(), 200); assert.ok(!JSON.stringify(await own.json()).includes('operator_observation'));
    const after = await command('inspect');
    assert.deepEqual(after.mock_upstream_calls, before.mock_upstream_calls);
    assert.equal(after.mock_upstream_calls.total, 1); assert.equal(after.mock_upstream_calls.GET, 0);
    assert.equal(after.mock_upstream_calls.classifier_POST, 0);
    assert.equal(after.health_store_sha256, before.health_store_sha256);
    assert.deepEqual(after.original_evidence_sha256, before.original_evidence_sha256);
    for (const key of ['known_api_usd', 'reserved_api_usd', 'unknown_events', 'control']) assert.deepEqual(after[key], before[key]);
    report.observations.push({ stage: 'completed_task_reads', before, after });
    report.checks.push({ behavior: 'operator_reads_add_no_calls_fees_or_changes_to_health_and_original_task_evidence', passed: true });
    assert.deepEqual(report.page_errors, []); assert.deepEqual(report.console_errors, []);
    assert.deepEqual(report.blocked_browser_requests, []); assert.deepEqual(after.blocked_server_socket_attempts, []);
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed'; report.failure = { name: error.name, message: error.message, stack: error.stack };
    if (page) { await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {}); report.screenshots.push('failure.png'); }
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    report.fixture_exit = await fixture.stop();
    await fs.writeFile(path.join(output, 'desktop-receipt.json'), JSON.stringify(report, null, 2));
    process.stdout.write(JSON.stringify({ status: report.status, output, checks: report.checks.length }) + '\n');
  }
}
main().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
