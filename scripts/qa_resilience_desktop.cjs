#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { startFixture } = require('./qa_fixture_ipc.cjs');
const { createHash } = require('node:crypto');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const ROOT = path.resolve(__dirname, '..'), CORE = process.env.CLOUDYFAN_CORE_ROOT, OUT = path.resolve(process.argv[2]);
const report = {scope:'actual Chrome desktop and loopback HTTP; real synthetic backup host; MockTransport upstream',
  viewport:{width:1280,height:800},steps:[],requests:[],responses:[],blocked:[],page_errors:[],console:[]};
let fixture, browser, page, origin;
async function save(){await fs.writeFile(path.join(OUT,'desktop-report.json'),JSON.stringify(report,null,2));}
async function control(command){fixture.send({command});return (await fixture.next()).state;}
async function shot(name){await page.screenshot({path:path.join(OUT,name+'.png')});await fs.writeFile(path.join(OUT,name+'.aria.txt'),await page.locator('body').ariaSnapshot());}
async function step(name,action){console.log('START '+name);await action();report.steps.push({name,passed:true});await save();console.log('PASS '+name);}
async function choose(selector,value){await page.locator(selector).focus();await page.locator(selector).selectOption(value);}
async function nextPool(){return page.waitForResponse(r=>r.url().startsWith(origin+'/monitor/pools?')&&r.status()===200);}
async function main(){
  assert.ok(CORE);await fs.mkdir(OUT,{recursive:false});
  report.started_at=new Date().toISOString();
  const sources=['src/cloudyfan_monitor/static/app.js','src/cloudyfan_monitor/static/candidates.js','src/cloudyfan_monitor/candidate_observation.py','scripts/qa_resilience_desktop.cjs','scripts/qa_resilience_http.py','tests/resilience_fixture.py'];
  report.source_sha256=Object.fromEntries(await Promise.all(sources.map(async p=>[p,createHash('sha256').update(await fs.readFile(path.join(ROOT,p))).digest('hex')])));
  fixture=startFixture(path.join(CORE,'.venv/bin/python'),[path.join(__dirname,'qa_resilience_http.py'),'--output',OUT],{
    cwd:ROOT,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[path.join(ROOT,'src'),path.join(CORE,'src')].join(path.delimiter)},responseTimeoutMs:30000});
  const ready=await fixture.next();assert.equal(ready.type,'ready');origin=ready.origin;
  browser=await chromium.launch({channel:'chrome',headless:true});report.browser=browser.version();
  const context=await browser.newContext({viewport:report.viewport,serviceWorkers:'block'});
  await context.route('**/*',async route=>{if(new URL(route.request().url()).origin===origin)return route.continue();report.blocked.push(route.request().url());return route.abort();});
  page=await context.newPage();page.setDefaultTimeout(10000);
  page.on('request',r=>report.requests.push({method:r.method(),path:new URL(r.url()).pathname}));
  page.on('response',r=>report.responses.push({path:new URL(r.url()).pathname,status:r.status()}));
  page.on('pageerror',e=>report.page_errors.push(e.message));
  page.on('console',m=>{if(['warning','error'].includes(m.type()))report.console.push(m.text());});
  await step('operator_login_uses_actual_desktop_form',async()=>{
    await page.goto(origin);await page.locator('#logout').click();await page.locator('#token').fill(ready.operator_token);
    await page.locator('#login-form button[type="submit"]').click();await page.locator('#login').waitFor({state:'hidden'});
    assert.equal(await page.locator('#token').inputValue(),'');
    await page.locator('a[data-page="pools"]').click();await page.locator('[data-candidate-health-status]').first().waitFor();
  });
  await step('periodic_poll_keeps_multiple_candidate_expansions_and_summary_focus',async()=>{
    const rows=[page.locator('[data-candidate-evidence="open_source:0"]'),page.locator('[data-candidate-evidence="mid:0"]')];
    for(const row of rows){await row.locator('summary').focus();await page.keyboard.press('Enter');}
    await rows[0].locator('summary').focus();
    const update=nextPool();await (await update).finished();
    await page.waitForTimeout(100);
    for(const row of rows)assert.equal(await row.locator('details').getAttribute('open'),'');
    assert.equal(await rows[0].locator('summary').evaluate(e=>e===document.activeElement),true);
    await shot('candidate-poll-focus');
  });
  await step('candidate_failure_keeps_original_time_and_other_live_task_updates',async()=>{
    const before=await control('hold');assert.equal(before.long_task_held,true);
    await control('fail_pool');
    await page.waitForResponse(r=>r.url().startsWith(origin+'/monitor/pools?')&&r.status()===503);
    await page.getByText('本次讀取失敗，以下是上次候選觀測的舊資料。',{exact:true}).waitFor();
    assert.match(await page.locator('#candidate-context').innerText(),/原判定時點/);
    await page.locator('a[data-page="tasks"]').click();
    await page.locator('tr[data-task-run="resilience-long"]').waitFor();
    const completed=page.locator('tr[data-task-run="resilience-completed"] button[data-task-trace-toggle]');
    await completed.focus();await page.keyboard.press('Enter');
    const after=await control('state');assert.equal(after.long_task_held,true);
    assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
    await shot('history-during-held-model-and-pool-failure');
    await control('restore_pool');await page.locator('a[data-page="pools"]').click();
    const refreshed=nextPool();await page.locator('#reload').click();await (await refreshed).finished();
    await page.getByText('本次讀取失敗，以下是上次候選觀測的舊資料。',{exact:true}).waitFor({state:'hidden'});
    await control('release');
  });
  await step('partial_and_whole_evidence_failure_keep_safe_controls_and_focus',async()=>{
    await control('damage_candidate');
    let update=nextPool();await page.locator('#reload').click();await (await update).finished();
    await page.getByText('部分候選證據無法確認，其餘可驗證內容仍列於下方。',{exact:true}).waitFor();
    assert.match(await page.locator('[data-candidate-evidence="open_source:0"]').innerText(),/證據無法確認/);
    assert.match(await page.locator('[data-candidate-evidence="mid:0"]').innerText(),/三維證據已記錄/);
    assert.ok(!(await page.locator('body').innerText()).includes('PRIVATE_UPSTREAM_FAILURE_MARKER'));
    await control('restore_candidate');
    update=nextPool();await page.locator('#reload').click();await (await update).finished();
    const summary=page.locator('[data-candidate-evidence="mid:0"] summary');await summary.focus();
    await control('unavailable_contract');
    update=nextPool();await (await update).finished();
    await page.getByText('目前候選觀測不可用；沒有可確認的資格或健康判定。',{exact:true}).waitFor();
    assert.equal(await page.locator('#candidate-version').evaluate(e=>e===document.activeElement),true);
    const focusBox=await page.locator('#candidate-version').boundingBox();
    assert.ok(focusBox.y>=0&&focusBox.y<report.viewport.height,'Disappearing detail should return to a visible query control');
    assert.equal(await page.locator('[data-candidate-health-status="healthy"]').count(),0);
    await shot('whole-observation-unavailable-focus-fallback');
    await control('restore_contract');
    update=nextPool();await page.locator('#reload').click();await (await update).finished();
    await page.locator('[data-candidate-health-status="healthy"]').first().waitFor();
  });
  await step('pool_category_and_mode_context_keeps_selector_focus',async()=>{
    await control('fail_pool');
    const failed=page.waitForResponse(r=>r.url().includes('/monitor/pools?')&&r.status()===503);
    await choose('#candidate-version',ready.pools[1]);await (await failed).finished();
    await page.getByText('此查詢目前無法取得候選觀測。保留既有候選清單，不推定資格或授權。',{exact:true}).waitFor();
    assert.equal(await page.locator('.pool-grid .model-name').count(),0,'A failed new version query must not borrow the old version listing');
    await control('restore_pool');
    await choose('#candidate-version',ready.pools[1]);
    await page.locator('#candidate-context').getByText('未啟用版本',{exact:true}).waitFor();
    assert.equal(await page.locator('#candidate-version').evaluate(e=>e===document.activeElement),true);
    await choose('#candidate-version',ready.pools[0]);
    await page.locator('#candidate-context').getByText('目前啟用版本',{exact:true}).waitFor();
    await choose('#category','design-arena:models:website');
    await page.locator('#candidate-context').getByText('契約未登錄',{exact:true}).waitFor();
    assert.equal(await page.locator('#category').evaluate(e=>e===document.activeElement),true);
    await choose('#category',ready.category);
    await page.locator('[data-candidate-health-status="healthy"]').first().waitFor();
  });
  await step('late_old_poll_cannot_replace_new_health_or_selected_context',async()=>{
    const before=await control('state');
    const original=await (await context.request.get(origin+'/monitor/snapshot',{headers:{Authorization:'Bearer '+ready.operator_token}})).json();
    const originalTask=original.tasks.find(t=>t.run==='resilience-completed').operator_observation;
    await control('delay_pool');
    let delayed=false;
    for(let attempt=0;attempt<70;attempt++){
      if((await control('state')).pool_response_held){delayed=true;break;}
      await page.waitForTimeout(100);
    }
    assert.equal(delayed,true,'Scheduled poll did not enter the synthetic HTTP response gate');
    await control('advance_health');
    await choose('#candidate-mode','fixed_high');
    await page.locator('#candidate-context').getByText('契約未登錄',{exact:true}).waitFor();
    await choose('#candidate-mode','cloudyfan');
    await page.locator('[data-candidate-health-status="stale"]').first().waitFor();
    await control('release_pool');await page.waitForTimeout(150);
    assert.equal(await page.locator('#candidate-mode').inputValue(),'cloudyfan');
    assert.equal(await page.locator('[data-candidate-health-status="healthy"]').count(),0);
    await page.locator('a[data-page="tasks"]').click();
    const button=page.locator('tr[data-task-run="resilience-completed"] button[data-task-trace-toggle]');
    if(await button.getAttribute('aria-expanded')!=='true'){await button.focus();await page.keyboard.press('Enter');}
    await button.focus();
    const poll=page.waitForResponse(r=>r.url()===origin+'/monitor/snapshot'&&r.status()===200);await (await poll).finished();await page.waitForTimeout(100);
    assert.equal(await button.getAttribute('aria-expanded'),'true');
    assert.equal(await button.evaluate(e=>e===document.activeElement),true);
    const after=await (await context.request.get(origin+'/monitor/snapshot',{headers:{Authorization:'Bearer '+ready.operator_token}})).json();
    assert.deepEqual(after.tasks.find(t=>t.run==='resilience-completed').operator_observation,originalTask);
    const measured=await control('state');assert.deepEqual(measured.model_requests,before.model_requests);
    assert.deepEqual(measured.budget,before.budget);assert.deepEqual(measured.source_requests,[]);assert.deepEqual(measured.accounting_requests,[]);
    await shot('historical-reasons-after-current-health-expiry');
  });
  await step('desktop_client_credential_is_denied_and_operator_relogin_stays_private',async()=>{
    const visible=await page.locator('body').innerText();
    for(const marker of ['PRIVATE_RESILIENCE_BROWSER_PROMPT','PRIVATE_UPSTREAM_FAILURE_MARKER',ready.operator_token,ready.client_token])assert.ok(!visible.includes(marker));
    await page.locator('#logout').click();await page.locator('#token').fill(ready.client_token);
    await page.locator('#login-form button[type="submit"]').click();
    await page.locator('#login-error').getByText('登入憑證不正確或已失效',{exact:true}).waitFor();
    assert.equal(await page.locator('#login').isVisible(),true);
    await page.locator('#token').fill(ready.operator_token);await page.locator('#login-form button[type="submit"]').click();
    await page.locator('#login').waitFor({state:'hidden'});assert.equal(await page.locator('#token').inputValue(),'');
    await page.locator('a[data-page="pools"]').click();await page.locator('[data-candidate-health-status="stale"]').first().waitFor();
    const measured=await control('state');assert.equal(measured.model_mock_calls,3);
    assert.deepEqual(measured.source_requests,[]);assert.deepEqual(measured.accounting_requests,[]);
    await shot('operator-relogin-safe-view');
  });
  assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.blocked,[]);report.passed=true;
}
main().catch(async error=>{report.passed=false;report.failure={name:error.name,message:error.message};process.exitCode=1;console.error(error.stack);if(page)await shot('failure').catch(()=>{});}).finally(async()=>{
  if(browser)await browser.close();
  if(fixture){
    report.fixture_exit=await fixture.stop();
    if(report.fixture_exit.code!==0||report.fixture_exit.timed_out||report.fixture_exit.cleanup_failed||report.fixture_exit.stderr_present){report.passed=false;process.exitCode=1;}
  }
  report.finished_at=new Date().toISOString();await save();
  console.log(JSON.stringify({passed:report.passed,steps:report.steps.length,report:path.join(OUT,'desktop-report.json')}));
});
