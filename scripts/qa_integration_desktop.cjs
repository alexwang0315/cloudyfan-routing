#!/usr/bin/env node
'use strict';
// Actual desktop only, using existing local Playwright + Chrome; no downloads.
const { chromium } = require(process.env.CLOUDYFAN_PLAYWRIGHT_MODULE || process.env.PLAYWRIGHT_PATH ||
  '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const { createHash, randomUUID } = require('node:crypto');
const { startFixture } = require('./qa_fixture_ipc.cjs');
const fs = require('node:fs/promises');
const assert = require('node:assert/strict');
const path = require('node:path');
const ROOT = path.resolve(__dirname, '..');
const CORE = process.env.CLOUDYFAN_CORE_ROOT || '/Users/alexwang0315/Documents/CloudyFan/cloudyfan-jev-core';
const OUT = process.argv[2] ? path.resolve(process.argv[2]) : path.join(ROOT, 'runtime/integration-desktop', randomUUID().slice(0,12));
let ORIGIN, fixture;
const report = {started_at:new Date().toISOString(), viewport:{width:1280,height:800},
  mobile:'stopped_by_user', steps:[], requests:[], responses:[], blocked:[], console:[], page_errors:[]};
let browser, page;
async function fixtureCommand(command) {
  fixture.send({command});
  return fixture.next();
}
async function save() { await fs.writeFile(path.join(OUT,'desktop-report.json'),JSON.stringify(report,null,2)); }
async function step(name, action) {
  console.log('START '+name);
  await action();
  report.steps.push({name,passed:true,at:new Date().toISOString()});
  await save();
  console.log('PASS '+name);
}
async function snapshot(name) {
  await fs.writeFile(path.join(OUT,name+'.aria.txt'),await page.locator('body').ariaSnapshot());
  await page.screenshot({path:path.join(OUT,name+'.png'),fullPage:false});
}
async function reloadData() {
  const pending=page.waitForResponse(r=>r.url()===ORIGIN+'/monitor/snapshot' && r.status()===200);
  await page.getByRole('button',{name:'重新讀取 ↻'}).click();
  return (await pending).json();
}
async function main() {
  await fs.mkdir(path.dirname(OUT),{recursive:true});
  await fs.mkdir(OUT);
  fixture=startFixture(path.join(CORE,'.venv/bin/python'),[path.join(__dirname,'qa_integration_http.py'),'--output',OUT],{
    cwd:ROOT,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',
      PYTHONPATH:[path.join(ROOT,'src'),path.join(CORE,'src')].join(path.delimiter)},responseTimeoutMs:30000});
  const ready=await fixture.next();
  assert.equal(ready.type,'ready');ORIGIN=ready.origin;report.origin=ORIGIN;
  report.scope='disposable synthetic data; actual desktop + loopback HTTP; MockTransport model and metadata upstream';
  const sourcePaths=['scripts/qa_integration_http.py','scripts/qa_integration_desktop.cjs','scripts/qa_fixture_ipc.cjs',
    'tests/test_local_http_integration.py','tests/test_sources.py','src/cloudyfan_monitor/static/app.js'];
  report.source_sha256=Object.fromEntries(await Promise.all(sourcePaths.map(async name=>
    [name,createHash('sha256').update(await fs.readFile(path.join(ROOT,name))).digest('hex')])));
  browser=await chromium.launch({channel:'chrome',headless:true});
  report.browser=browser.version();
  const context=await browser.newContext({viewport:report.viewport,locale:'zh-TW',serviceWorkers:'block'});
  await context.route('**/*',async route=>{
    const request=route.request(), url=new URL(request.url());
    if(url.origin===ORIGIN) await route.continue();
    else {report.blocked.push({method:request.method(),url:url.href});await route.abort();}
  });
  page=await context.newPage();
  page.setDefaultTimeout(20000);
  page.on('request',r=>report.requests.push({method:r.method(),url:r.url()}));
  page.on('response',r=>report.responses.push({url:r.url(),status:r.status()}));
  page.on('console',m=>{if(['error','warning'].includes(m.type()))report.console.push({kind:m.type(),message:m.text()});});
  page.on('pageerror',e=>report.page_errors.push(e.message));
  let baseline, publishedPool;
  await step('overview_busy_and_expired_history',async()=>{
    await page.goto(ORIGIN,{waitUntil:'domcontentloaded',timeout:30000});
    await page.getByRole('heading',{name:'路由運作，一眼掌握。'}).waitFor();
    baseline=await reloadData();
    assert.match(await page.locator('#environment').innerText(),/模擬/);
    assert.equal(baseline.summary.passed,5);
    assert.equal(baseline.tasks.find(t=>t.run==='browser-busy').status,'running');
    const expired=baseline.tasks.find(t=>t.run==='browser-expired-history');
    assert.equal(expired.status,'expired');
    assert.equal(expired.complete_api_usd,'0.0001');
    report.baseline={summary:baseline.summary,control:baseline.control,known_usd:baseline.ledger.known_usd};
    await snapshot('desktop-overview-busy');
  });
  await step('global_mode_keeps_frozen_tasks',async()=>{
    await page.getByRole('combobox',{name:'全域模式',exact:true}).selectOption('low_first');
    await page.getByRole('button',{name:'套用',exact:true}).click();
    await page.getByRole('heading',{name:'變更新任務的路由模式'}).waitFor();
    await snapshot('desktop-mode-confirm');
    await page.getByRole('button',{name:'確認套用',exact:true}).click();
    await page.getByRole('heading',{name:'全便宜模型優先',exact:true}).waitFor();
    const changed=await reloadData();
    assert.equal(changed.control.mode,'low_first');
    assert.deepEqual(changed.tasks.map(t=>[t.client,t.run,t.mode,t.pool]),baseline.tasks.map(t=>[t.client,t.run,t.mode,t.pool]));
    await page.getByRole('combobox',{name:'全域模式',exact:true}).selectOption('cloudyfan');
    await page.getByRole('button',{name:'套用',exact:true}).click();
    await page.getByRole('button',{name:'確認套用',exact:true}).click();
    await page.getByRole('heading',{name:'CloudyFan MRC',exact:true}).waitFor();
  });
  await step('client_override_isolated_and_restored',async()=>{
    await page.getByRole('link',{name:'⌘ 連線客戶端'}).click();
    await page.getByRole('heading',{name:'每個客戶端，各自看得清。'}).waitFor();
    const studio=page.getByRole('article').filter({has:page.getByRole('heading',{name:'Studio 工作台',exact:true})});
    await studio.getByRole('combobox',{name:'客戶端模式'}).selectOption('open_source_first');
    await studio.getByRole('button',{name:'套用',exact:true}).click();
    await page.getByRole('button',{name:'確認套用',exact:true}).click();
    await page.getByText('實際新任務模式：全開源模型優先',{exact:false}).waitFor();
    const changed=await reloadData();
    assert.equal(changed.clients.find(c=>c.id==='studio').effective_mode,'open_source_first');
    assert.equal(changed.clients.find(c=>c.id==='agent').effective_mode,'cloudyfan');
    await snapshot('desktop-client-override');
    await studio.getByRole('combobox',{name:'客戶端模式'}).selectOption('');
    await studio.getByRole('button',{name:'套用',exact:true}).click();
    await page.getByRole('button',{name:'確認套用',exact:true}).click();
    await page.getByText('實際新任務模式：全開源模型優先',{exact:false}).waitFor({state:'hidden'});
  });
  await step('23_category_contract_matrix_and_missing_slots',async()=>{
    await page.getByRole('link',{name:'▦ 候選池與契約 92'}).click();
    const heading=page.getByRole('heading',{name:'類別 × 四模式契約',exact:true});
    await heading.waitFor();
    const matrix=page.locator('section').filter({has:heading});
    assert.equal(await matrix.locator('tbody tr').count(),23);
    assert.equal(await matrix.getByText('契約已註冊',{exact:true}).count(),4);
    assert.equal(await matrix.getByText('未支援',{exact:true}).count(),88);
    await heading.scrollIntoViewIfNeeded();
    await snapshot('desktop-contract-matrix');
    await page.getByRole('combobox',{name:'任務分類',exact:true}).selectOption('design-arena:models:audiorealism');
    assert.equal(await page.locator('.pool-grid').getByText('尚無可對應的候選。',{exact:false}).count(),12);
    await page.getByRole('heading',{name:'23 類候選池視圖',exact:true}).scrollIntoViewIfNeeded();
    await snapshot('desktop-missing-candidates');
    await page.getByRole('combobox',{name:'任務分類',exact:true}).selectOption('artificial-analysis:coding_index');
  });
  await step('partial_source_diff_publish_separate_from_activation',async()=>{
    const pending=page.waitForResponse(r=>r.url()===ORIGIN+'/monitor/pools/refresh');
    await page.getByRole('button',{name:'手動更新來源',exact:true}).click();
    const response=await pending, body=await response.json();
    assert.equal(response.status(),200);
    assert.equal(body.completeness.status,'partial');
    assert.deepEqual([body.completeness.attempted,body.completeness.succeeded,body.completeness.failed,body.completeness.deferred],[5,4,1,1]);
    await page.getByRole('heading',{name:'候選更新已準備，檢查差異',exact:true}).waitFor();
    const dialog=await page.getByRole('dialog').ariaSnapshot();
    assert.match(dialog,/部分更新/);
    await snapshot('desktop-source-diff');
    await page.getByRole('button',{name:'確認套用',exact:true}).click();
    await page.getByRole('dialog').waitFor({state:'hidden'});
    await page.locator('#activate-pool:disabled').waitFor();
    const changed=await reloadData();
    assert.equal(changed.control.routing_pool,baseline.control.routing_pool);
    assert.notEqual(changed.control.catalog_pool,baseline.control.catalog_pool);
    publishedPool=changed.control.catalog_pool;
    report.partial_source=body.completeness;
    await snapshot('desktop-source-partial-published');
  });
  await step('main_source_failure_keeps_published_candidate',async()=>{
    const pending=page.waitForResponse(r=>r.url()===ORIGIN+'/monitor/pools/refresh');
    await page.getByRole('button',{name:'手動更新來源',exact:true}).click();
    const response=await pending, body=await response.json();
    assert.equal(response.status(),503);
    assert.equal(body.completeness.status,'failed');
    assert.equal(body.completeness.attempted,1);
    await page.getByRole('alert').getByText('更新失敗，現有版本保留：',{exact:false}).waitFor();
    await snapshot('desktop-source-main-failure');
    const changed=await reloadData();
    assert.equal(changed.control.catalog_pool,publishedPool);
    assert.equal(changed.control.routing_pool,baseline.control.routing_pool);
    report.failed_source=body.completeness;
  });
  await step('task_expiry_and_history_and_ledger_attribution',async()=>{
    await page.getByRole('link',{name:'↳ 任務與升級'}).click();
    await page.getByRole('heading',{name:'任務與失敗升級',exact:true}).waitFor();
    const expired=page.getByRole('row').filter({hasText:'browser-expired-history'});
    assert.match(await expired.innerText(),/已到期/);
    assert.match(await expired.innerText(),/歷史統計保留/);
    assert.match(await expired.innerText(),/0.000100/);
    assert.match(await page.getByRole('row').filter({hasText:'browser-busy'}).innerText(),/執行中/);
    await snapshot('desktop-tasks-expired-and-busy');
    await page.getByRole('link',{name:'▤ 費用帳本'}).click();
    await page.getByRole('heading',{name:'費用歸屬',exact:true}).waitFor();
    await page.getByRole('cell',{name:'帳戶歷史／未歸屬費用',exact:true}).waitFor();
    await snapshot('desktop-ledger-attribution');
  });
  await step('accepted_work_finishes_without_polling_dispatch',async()=>{
    const before=(await fixtureCommand('state')).state;
    assert.equal(before.model_mock_calls,6);
    const released=await fixtureCommand('release');
    assert.equal(released.type,'released');
    const state=released.state;
    assert.equal(state.tasks.find(t=>t.run==='browser-busy').status,'passed');
    assert.equal(state.model_mock_calls,6);
    assert.equal(state.source_mock_calls,6);
    await page.getByRole('link',{name:'◫ 運作總覽'}).click();
    const final=await reloadData();
    assert.equal(final.summary.passed,6);
    assert.equal(final.tasks.find(t=>t.run==='browser-expired-history').status,'expired');
    assert.equal(final.ledger.known_usd,'0.0047');
    report.final={summary:final.summary,control:final.control,known_usd:final.ledger.known_usd,
      model_mock_calls:state.model_mock_calls,source_mock_calls:state.source_mock_calls};
    await snapshot('desktop-overview-finished');
  });
  assert.equal(report.page_errors.length,0);
  assert.equal(report.blocked.length,0);
  const receipt=JSON.parse(await fs.readFile(path.join(OUT,'http-receipt.json'),'utf8'));
  assert.equal(receipt.mock_upstream_call_count,6);
  assert.equal(receipt.source_mock_requests.length,6);
  assert.equal(receipt.blocked_socket_attempts.length,0);
  assert.ok(receipt.socket_attempts.length>0);
  assert.ok(receipt.http_responses.length>0);
  assert.ok(receipt.http_responses.every(item=>item.remote_addr==='127.0.0.1'));
  const expected=report.console.filter(item=>item.kind==='error'&&/status of 503/.test(item.message));
  assert.equal(expected.length,1);
  assert.equal(report.console.length,1);
  report.expected_console_resource_errors=expected;
  report.passed=true;
}
main().catch(async error=>{
  report.passed=false;report.failure={name:error.name,message:error.message,stack:error.stack};
  console.error(error.stack);process.exitCode=1;
}).finally(async()=>{
  if(browser) await browser.close().catch(()=>{
    report.browser_cleanup_error='Browser cleanup failed';report.passed=false;process.exitCode=1;
  });
  if(fixture){
    const outcome=await fixture.stop();
    report.fixture_exit=outcome;
    if(outcome.timed_out||outcome.cleanup_failed||outcome.code!==0){report.passed=false;process.exitCode=1;}
    // A safe marker avoids exporting library exception text with request data.
    if(outcome.stderr_present)report.fixture_error='Fixture stderr present; inspect locally';
  }
  report.finished_at=new Date().toISOString();await save();
  console.log(JSON.stringify({passed:report.passed,steps:report.steps.length,report:path.join(OUT,'desktop-report.json')}));
});
