#!/usr/bin/env node
'use strict';
// Real desktop + loopback service; every model call is a MockTransport response.
const {chromium}=require(process.env.CLOUDYFAN_PLAYWRIGHT_MODULE || process.env.PLAYWRIGHT_PATH ||
  '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {startFixture}=require('./qa_fixture_ipc.cjs');
const fs=require('node:fs/promises');
const path=require('node:path');
const assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
const ROOT=path.resolve(__dirname,'..');
const CORE=process.env.CLOUDYFAN_CORE_ROOT;
const OUT=path.resolve(process.argv[2]);
let fixture,browser,page,origin,baseline;
const report={scope:'synthetic frozen evidence; actual desktop + loopback HTTP; MockTransport models',
  started_at:new Date().toISOString(),steps:[],blocked:[],page_errors:[],console:[]};
async function save(){await fs.writeFile(path.join(OUT,'desktop-report.json'),JSON.stringify(report,null,2));}
async function step(name,action){console.log('START '+name);await action();report.steps.push({name,passed:true});await save();console.log('PASS '+name);}
async function snapshot(name){await page.screenshot({path:path.join(OUT,name+'.png'),fullPage:false});await fs.writeFile(path.join(OUT,name+'.aria.txt'),await page.locator('body').ariaSnapshot());}
async function reload(){const pending=page.waitForResponse(r=>r.url()===origin+'/monitor/snapshot'&&r.status()===200);await page.getByRole('button',{name:'重新讀取 ↻'}).click();return (await pending).json();}
async function command(value){fixture.send({command:value});return fixture.next();}
async function main(){
  assert.ok(CORE,'CLOUDYFAN_CORE_ROOT must select the approved runtime');
  await fs.mkdir(OUT,{recursive:false});
  fixture=startFixture(path.join(CORE,'.venv/bin/python'),[path.join(ROOT,'scripts/qa_history_http.py'),'--output',OUT],{
    cwd:ROOT,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[path.join(ROOT,'src'),path.join(CORE,'src')].join(path.delimiter)},responseTimeoutMs:30000});
  const ready=await fixture.next();assert.equal(ready.type,'ready');origin=ready.origin;baseline=ready.state;
  report.origin=origin;report.baseline_mock_calls=baseline.model_mock_calls;
  const sources=['src/cloudyfan_monitor/history_observation.py','src/cloudyfan_monitor/static/trace.js',
    'src/cloudyfan_monitor/app.py','src/cloudyfan_monitor/integration.py',
    'src/cloudyfan_monitor/observation_fields.py','src/cloudyfan_monitor/tracing.py',
    'scripts/qa_history_http.py','scripts/qa_history_desktop.cjs','tests/history_fixture.py','tests/test_history_observation.py',
    'tests/_backup_fixture.py','tests/test_category_host_integration.py','tests/candidate_observation_fixture.py'];
  report.source_sha256=Object.fromEntries(await Promise.all(sources.map(async source=>[source,createHash('sha256').update(await fs.readFile(path.join(ROOT,source))).digest('hex')])));
  browser=await chromium.launch({channel:'chrome',headless:true});
  report.browser=browser.version();
  const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-TW',serviceWorkers:'block'});
  await context.route('**/*',async route=>{if(new URL(route.request().url()).origin===origin)await route.continue();else{report.blocked.push(route.request().url());await route.abort();}});
  page=await context.newPage();page.setDefaultTimeout(10000);
  page.on('pageerror',error=>report.page_errors.push(error.message));
  page.on('console',message=>{if(['warning','error'].includes(message.type()))report.console.push(message.text());});
  await page.goto(origin,{waitUntil:'domcontentloaded'});
  await page.getByRole('heading',{name:'路由運作，一眼掌握。'}).waitFor();
  const initial=await reload();
  assert.equal(initial.tasks.length,9);
  await page.getByRole('link',{name:'↳ 任務與升級'}).click();
  const cases=[['classified',/保守淨節省足夠，已呼叫 Jev/],['direct',/缺少精確分類校準/],
    ['economic',/已有校準，但保守淨節省不足/],['availability',/供應商可用性故障後的同池備援/],
    ['quality-chain',/中／高階段/],['first-pass',/未進入此階段/],['legacy',/未提供逐候選歷史證據/],
    ['qcs-direct',/缺少完整分類流程耗時估計/],['qcs-classified',/保守淨節省足夠，已呼叫 Jev/]];
  for(const [name,expected] of cases){
    await step('historical_'+name,async()=>{
      const button=page.locator('tr[data-task-run="history-'+name+'"]').locator('[data-task-trace-toggle]');
      await button.focus();await page.keyboard.press('Enter');
      const id=await button.getAttribute('aria-controls');
      const detail=page.locator('[id='+JSON.stringify(id)+']');
      await detail.getByRole('heading',{name:'任務當時的決策依據',exact:true}).waitFor();
      assert.match(await detail.innerText(),expected);
      assert.doesNotMatch(await detail.innerText(),/PRIVATE-HISTORY-PROMPT|fixture\/|Bearer/);
      if(name==='classified'){
        assert.equal(await detail.locator('[data-trace-decision]').count(),1,
          'Only the saved paid classifier call belongs in Jev call details');
        assert.match(await detail.innerText(),/完整流程估計/);
        assert.match(await detail.innerText(),/分類實付 API 費用/);
        assert.match(await detail.innerText(),/0\.001/);
        assert.match(await detail.innerText(),/分布不是答對機率/);
      }
      if(['classified','quality-chain','qcs-classified'].includes(name)){
        const calls=detail.locator('[data-trace-decision]');
        const previous=async call=>call.locator('dt').filter({hasText:/^前次模型池$/}).locator('..').locator('dd').innerText();
        const trigger=async call=>call.locator('dt').filter({hasText:/^觸發原因$/}).locator('..').locator('dd').innerText();
        assert.equal(await previous(calls.nth(0)),'開源池');
        assert.equal(await trigger(calls.nth(0)),'前次公開驗收未通過');
        if(name==='quality-chain'){
          assert.equal(await calls.count(),2);
          assert.equal(await previous(calls.nth(1)),'低分池');
          assert.equal(await trigger(calls.nth(1)),'前次公開驗收未通過');
        }
      }
      if(['classified','qcs-direct','qcs-classified'].includes(name)){
        assert.equal(await detail.locator('dt').filter({hasText:/^原分類校準版本$/}).locator('..').locator('dd').first().innerText(),'未知');
        assert.match(await detail.innerText(),/原校準證據指紋/);
        assert.match(await detail.innerText(),/原分類協定指紋/);
      }
      await detail.locator('[data-history-observation]').evaluate(node=>node.scrollIntoView({block:'start'}));
      await snapshot('desktop-history-'+name);
      if(['classified','availability'].includes(name)){
        await detail.locator('[data-history-stage]').nth(1).evaluate(node=>node.scrollIntoView({block:'start'}));
        await snapshot('desktop-history-'+name+'-reason');
      }
    });
  }
  await step('refresh_preserves_expansion_and_keyboard_focus',async()=>{
    const button=page.locator('tr[data-task-run="history-classified"]').locator('[data-task-trace-toggle]');
    const key=await button.getAttribute('data-task-trace-toggle');
    await button.focus();
    // Wait for the existing periodic refresh while the detail toggle owns focus.
    await page.waitForResponse(r=>r.url()===origin+'/monitor/snapshot'&&r.status()===200,{timeout:15000});
    await page.waitForFunction(key=>document.activeElement?.dataset.taskTraceToggle===key,key);
    assert.equal(await button.getAttribute('aria-expanded'),'true');
    assert.equal(await page.locator('[data-task-trace-toggle][aria-expanded="true"]').count(),9);
  });
  await step('historical_health_and_fees_stay_frozen_on_read',async()=>{
    await command('health');
    const after=await reload();
    assert.deepEqual(after.tasks.map(t=>t.operator_observation),initial.tasks.map(t=>t.operator_observation));
    const final=(await command('state')).state;
    assert.equal(final.model_mock_calls,baseline.model_mock_calls);
    assert.deepEqual(final.budgets,baseline.budgets);
    report.final_mock_calls=final.model_mock_calls;
  });
  assert.equal(report.blocked.length,0);assert.equal(report.page_errors.length,0);assert.equal(report.console.length,0);
  report.passed=true;
}
main().catch(async error=>{report.passed=false;report.failure={name:error.name,message:error.message};console.error(error.stack);process.exitCode=1;if(page)await snapshot('desktop-failure').catch(()=>{});}).finally(async()=>{
  if(browser)await browser.close().catch(()=>{process.exitCode=1;report.passed=false;});
  if(fixture){report.fixture_exit=await fixture.stop();if(report.fixture_exit.code!==0||report.fixture_exit.timed_out){process.exitCode=1;report.passed=false;}}
  report.finished_at=new Date().toISOString();await save();console.log(JSON.stringify({passed:report.passed,report:path.join(OUT,'desktop-report.json')}));
});
