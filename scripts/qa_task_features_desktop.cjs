#!/usr/bin/env node
'use strict';
// Real own headed Chrome and HTTP; all models/sources are MockTransport only.
const {chromium}=require(process.env.CLOUDYFAN_PLAYWRIGHT_MODULE || process.env.PLAYWRIGHT_PATH ||
  '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {startFixture}=require('./qa_fixture_ipc.cjs');
const fs=require('node:fs/promises');
const path=require('node:path');
const assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const ROOT=path.resolve(__dirname,'..'),CORE=process.env.CLOUDYFAN_CORE_ROOT,OUT=path.resolve(process.argv[2]);
let fixture,browser,page,origin,baseline;
const report={scope:'Focused39 only; real headed Chrome1280x900 + HTTP, MockTransport upstreams',
  started_at:new Date().toISOString(),steps:[],blocked:[],page_errors:[],console:[],network:[],driver_pid:process.pid};
async function save(){await fs.writeFile(path.join(OUT,'desktop-report.json'),JSON.stringify(report,null,2));}
async function step(name,action){console.log('START '+name);await action();report.steps.push({name,passed:true});await save();console.log('PASS '+name);}
async function snap(name){await page.screenshot({path:path.join(OUT,name+'.png'),fullPage:false});await fs.writeFile(path.join(OUT,name+'.aria.txt'),await page.locator('body').ariaSnapshot());}
async function command(value){fixture.send({command:value});return fixture.next();}
function childPids(){const rows=execFileSync('/bin/ps',['-axo','pid=,ppid=,comm='],{encoding:'utf8'}).trim().split('\n').map(line=>line.trim().split(/\s+/));
  const ids=new Set([process.pid]);let changed=true;while(changed){changed=false;for(const row of rows){if(ids.has(Number(row[1]))&&!ids.has(Number(row[0]))){ids.add(Number(row[0]));changed=true;}}}return [...ids].filter(id=>id!==process.pid);}
async function main(){
  process.umask(0o077);assert.ok(CORE,'Explicit approved Core required');await fs.mkdir(OUT,{recursive:false,mode:0o700});
  fixture=startFixture(path.join(CORE,'.venv/bin/python'),[path.join(ROOT,'scripts/qa_task_features_http.py'),'--output',OUT],{
    cwd:ROOT,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[path.join(ROOT,'src'),path.join(CORE,'src')].join(path.delimiter)},responseTimeoutMs:15000});
  const ready=await fixture.next();assert.equal(ready.type,'ready');origin=ready.origin;baseline=ready.state;report.fixture_pid=ready.fixture_pid;
  assert.equal(baseline.private_prompt_canary_present,true);assert.equal(baseline.model_calls,1);
  browser=await chromium.launch({channel:'chrome',headless:false});report.browser=browser.version();
  const context=await browser.newContext({viewport:{width:1280,height:900},locale:'zh-TW',serviceWorkers:'block'});
  await context.route('**/*',async route=>{if(new URL(route.request().url()).origin===origin)await route.continue();else{report.blocked.push(route.request().url());await route.abort();}});
  page=await context.newPage();page.setDefaultTimeout(6000);report.owned_child_pids=childPids();
  page.on('pageerror',error=>report.page_errors.push(error.message));
  page.on('console',message=>{if(['warning','error'].includes(message.type()))report.console.push(message.text());});
  page.on('response',response=>report.network.push({method:response.request().method(),path:new URL(response.url()).pathname,status:response.status()}));
  await page.goto(origin,{waitUntil:'domcontentloaded'});
  await page.getByRole('heading',{name:'路由運作，一眼掌握。'}).waitFor();
  await page.getByRole('link',{name:'↳ 任務與升級'}).click();
  for(const run of ['features-queued','features-complete','features-legacy']){
    await step(run+'_creation_observation',async()=>{
      const button=page.locator('tr[data-task-run="'+run+'"]').locator('[data-task-trace-toggle]');
      await button.focus();await page.keyboard.press('Enter');
      const id=await button.getAttribute('aria-controls'),detail=page.locator('[id='+JSON.stringify(id)+']');
      await detail.getByRole('heading',{name:'任務建立時特徵',exact:true}).waitFor();
      if(run!=='features-legacy'){
        const disclosure=detail.getByText('查看單位、來源與未知原因',{exact:true});
        await disclosure.focus();await page.keyboard.press('Enter');
        await detail.getByText('UTF-8 bytes',{exact:true}).waitFor();
        assert.match(await detail.innerText(),/Unicode codepoints/);assert.match(await detail.innerText(),/明示/);
        assert.match(await detail.innerText(),/未知/);assert.match(await detail.innerText(),/local-deterministic-v2/);
        const reason=detail.locator('[data-feature-reasons="input_tokens"] summary');
        await reason.focus();await page.keyboard.press('Enter');
        assert.match(await detail.innerText(),/未提供精確 tokenizer/);
        if(run==='features-queued'){
          const focused=await reason.getAttribute('id');
          await page.waitForResponse(response=>response.url()===origin+'/monitor/snapshot'&&response.status()===200,{timeout:15000});
          await page.waitForFunction(id=>document.activeElement?.id===id,focused);
          assert.equal(await detail.locator('[data-feature-reasons="input_tokens"]').getAttribute('open'),'');
        }
      }else assert.match(await detail.innerText(),/無法確認|未提供/);
      await detail.locator('[data-task-features]').scrollIntoViewIfNeeded();await snap(run);
      assert.doesNotMatch(await page.locator('body').innerText(),/PRIVATE_FEATURE_DESKTOP_CANARY|Bearer|receipts-primary|budget-primary\.sqlite/);
      assert.doesNotMatch(await page.locator('body').ariaSnapshot(),/PRIVATE_FEATURE_DESKTOP_CANARY|Bearer/);
    });
  }
  const final=(await command('state')).state;assert.deepEqual(final,baseline);report.final_state_unchanged=true;
  assert.equal(report.blocked.length,0);assert.equal(report.page_errors.length,0);assert.equal(report.console.length,0);
  report.passed=true;
}
main().catch(async error=>{report.passed=false;report.failure={name:error.name,message:error.message};process.exitCode=1;
  if(page)await snap('desktop-failure').catch(()=>{});
}).finally(async()=>{
  if(browser)await browser.close().catch(()=>{report.passed=false;process.exitCode=1;});
  if(fixture){report.fixture_exit=await fixture.stop();if(report.fixture_exit.code!==0||report.fixture_exit.timed_out||report.fixture_exit.stderr_present){report.passed=false;process.exitCode=1;}}
  report.owned_child_pids_exited=(report.owned_child_pids||[]).map(pid=>{let exited=false;try{process.kill(pid,0);}catch(error){exited=error.code==='ESRCH';}return{pid,exited};});
  if(report.owned_child_pids_exited.some(row=>!row.exited)){report.passed=false;process.exitCode=1;}
  report.finished_at=new Date().toISOString();await save();console.log(JSON.stringify({passed:report.passed,report:path.join(OUT,'desktop-report.json')}));
});
