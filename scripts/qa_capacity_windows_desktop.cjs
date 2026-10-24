#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {createHash,randomUUID} = require('node:crypto');
const {startFixture} = require('./qa_fixture_ipc.cjs');

async function main() {
  const root=path.resolve(__dirname,'..'),core=process.env.CLOUDYFAN_CORE_ROOT;
  assert.ok(core,'Use an explicitly matched Core root');
  const {chromium}=require(process.env.PLAYWRIGHT_PATH||'/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
  const output=process.argv[2]?path.resolve(process.argv[2]):path.join(root,'runtime/capacity-windows-desktop',randomUUID().slice(0,12));
  await fs.mkdir(path.dirname(output),{recursive:true});await fs.mkdir(output,{recursive:false,mode:0o700});
  const report={schema_version:1,status:'incomplete',viewport:{width:1280,height:800},checks:[],screenshots:[],
    http_responses:[],page_errors:[],console_errors:[],expected_http_errors:[],blocked_browser_requests:[],
    scope:'actual Chrome + loopback HTTP; real synthetic window/health/capacity host; MockTransport only; no provider recovery or capacity/SLO claim'};
  const fixture=startFixture(process.env.CLOUDYFAN_QA_PYTHON||path.join(core,'.venv/bin/python'),
    [path.join(__dirname,'qa_capacity_windows_http.py'),'--output',output],{cwd:root,
      env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[path.join(root,'src'),path.join(core,'src')].join(path.delimiter)}});
  let browser,page;
  const inspect=async(command={command:'inspect'})=>{fixture.send(command);return fixture.next();};
  const step=async(name,action)=>{await action();report.checks.push({name,passed:true});console.log('PASS '+name);};
  const shot=async name=>{await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});report.screenshots.push(name+'.png');
    await fs.writeFile(path.join(output,name+'.aria.txt'),await page.locator('body').ariaSnapshot());};
  try {
    const names=['src/cloudyfan_monitor/static/capacity.js','src/cloudyfan_monitor/static/capacity.css','src/cloudyfan_monitor/app.py',
      'src/cloudyfan_monitor/history_observation.py','src/cloudyfan_monitor/integration.py',
      'tests/candidate_health_fixture.py','scripts/qa_capacity_windows_http.py','scripts/qa_capacity_windows_desktop.cjs'];
    report.source_sha256=Object.fromEntries(await Promise.all(names.map(async name=>[name,createHash('sha256').update(await fs.readFile(path.join(root,name))).digest('hex')])));
    const coreNames=['capacity_dispatch.py','capacity.py','capacity_windows.py','client.py','budget.py','http_api.py','engine.py','local.py','scores/backup_runtime.py'];
    report.core_source_sha256=Object.fromEntries(await Promise.all(coreNames.map(async name=>[name,
      createHash('sha256').update(await fs.readFile(path.join(core,'src/cloudyfan_jev',name))).digest('hex')])));
    const ready=await fixture.next();assert.equal(ready.type,'ready');
    browser=await chromium.launch({channel:'chrome',headless:process.env.CLOUDYFAN_QA_HEADED!=='1'});report.browser=browser.version();
    const context=await browser.newContext({viewport:report.viewport,locale:'zh-TW',serviceWorkers:'block'});
    await context.route('**/*',route=>{if(new URL(route.request().url()).origin===ready.origin)return route.continue();
      report.blocked_browser_requests.push(new URL(route.request().url()).origin);return route.abort();});
    page=await context.newPage();page.setDefaultTimeout(6000);
    page.on('pageerror',()=>report.page_errors.push('JavaScript page error'));
    page.on('console',message=>{if(message.type()==='error')report.console_errors.push(message.text());});
    page.on('response',response=>report.http_responses.push({method:response.request().method(),path:new URL(response.url()).pathname,status:response.status()}));
    const headers=token=>({Authorization:'Bearer '+token});
    const task=(run)=>({run_id:run,category:ready.category,prompt:'Return only 1+1. PRIVATE_WINDOW_PROMPT_CANARY'});
    const operator=headers(ready.operator_token),client=headers(ready.credentials.studio);
    let response=await context.request.post(ready.origin+'/monitor/capacity/activate',{headers:operator,
      data:{revision:0,version:'synthetic-window-admission-v1'}});
    assert.equal(response.status(),200);
    await inspect({command:'scenario',name:'window'});
    response=await context.request.post(ready.origin+'/v1/tasks',{headers:client,data:task('window-fallback')});
    assert.equal(response.status(),200);assert.equal((await response.json()).status,'passed');
    const actual=await inspect();assert.equal(actual.model_requests.length,2);
    assert.equal(actual.window_snapshot.groups[0].not_before,actual.clock+30);
    await page.goto(ready.origin);await page.locator('#environment').filter({hasText:/模擬/}).waitFor();
    await page.locator('#logout').click();await page.locator('#token').fill(ready.operator_token);await page.getByRole('button',{name:'登入',exact:true}).click();
    await page.locator('#login[open]').waitFor({state:'hidden'});await page.locator('a[data-page="capacity"]').click();
    await step('actual_saved_retry_after_window_and_original_source_time_are_visible',async()=>{
      const panel=page.locator('#capacity-windows');await panel.waitFor();
      assert.match(await panel.innerText(),/原回應|原始回應/);
      assert.match(await panel.innerText(),/synthetic-windows-v1/);
      await panel.locator('[data-capacity-window]').first().locator('summary').click();
      assert.match(await panel.innerText(),/retry-after/);
      await shot('01-original-response-window');
    });
    report.status='passed';
  } catch(error) {
    report.status='failed';report.failure={name:error.name,message:error.message,stack:error.stack};process.exitCode=1;
    if(page)await shot('failure').catch(()=>{});
  } finally {
    if(browser)await browser.close();report.fixture_stop=await fixture.stop();
    if(report.source_sha256&&report.core_source_sha256) {
      const after=Object.fromEntries(await Promise.all(Object.keys(report.source_sha256).map(async name=>[name,
        createHash('sha256').update(await fs.readFile(path.join(root,name))).digest('hex')])));
      const coreAfter=Object.fromEntries(await Promise.all(Object.keys(report.core_source_sha256).map(async name=>[name,
        createHash('sha256').update(await fs.readFile(path.join(core,'src/cloudyfan_jev',name))).digest('hex')])));
      report.source_inputs_unchanged=JSON.stringify(after)===JSON.stringify(report.source_sha256)&&JSON.stringify(coreAfter)===JSON.stringify(report.core_source_sha256);
      if(!report.source_inputs_unchanged){report.status='failed';process.exitCode=1;report.input_failure='Source inputs changed during desktop QA';}
    }
    await fs.writeFile(path.join(output,'desktop-receipt.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({status:report.status,checks:report.checks.length,output}));
  }
}
main().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
