#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {execFileSync} = require('node:child_process');
const {startFixture} = require('./qa_fixture_ipc.cjs');

async function main() {
  const root=path.resolve(__dirname,'..'),core=process.env.CLOUDYFAN_CORE_ROOT;
  assert.ok(core,'An explicitly matched Core root is required');
  assert.ok(process.argv[2],'Use a new private output directory');
  const output=path.resolve(process.argv[2]);
  await fs.mkdir(path.dirname(output),{recursive:true});
  await fs.mkdir(output,{recursive:false,mode:0o700});
  const {chromium}=require(process.env.PLAYWRIGHT_PATH||'/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
  const owned=['src/cloudyfan_monitor/static/capacity.js','src/cloudyfan_monitor/static/capacity.css',
    'src/cloudyfan_monitor/static/queue.js','src/cloudyfan_monitor/static/queue.css',
    'scripts/qa_capacity_resilience_http.py','scripts/qa_capacity_resilience_desktop.cjs'];
  async function capture(directory,extra=[]) {
    const git=(...args)=>execFileSync('git',args,{cwd:directory,encoding:'utf8'}).trim();
    const names=[...new Set([...git('ls-files','-z').split('\0').filter(Boolean),...extra])].sort();
    const hashes={};
    for(const name of names) {
      const target=path.join(directory,name),state=await fs.lstat(target);
      const bytes=state.isSymbolicLink()?Buffer.from(await fs.readlink(target)):await fs.readFile(target);
      hashes[name]=createHash('sha256').update(bytes).digest('hex');
    }
    return {head:git('rev-parse','HEAD'),tree:git('rev-parse','HEAD^{tree}'),
      status:git('status','--porcelain=v1','--untracked-files=normal'),sha256:hashes};
  }
  const report={status:'incomplete',scope:'initial actual Chrome/HTTP storage-preservation display slice; real queue/Core/services; MockTransport only; Health/restart/full workflow pending',
    checks:[],setup_assertions:[],screenshots:[],page_errors:[],console_errors:[],http_responses:[],
    blocked_browser_requests:[],viewport:{width:1280,height:900},
    visual_regression:{status:'INCONCLUSIVE',reason:'No declared visual baseline; preserve actual rendered images'},
    accessibility_scope:'Initial display slice only; no screen-reader or WCAG certification'};
  const fixture=startFixture(path.join(core,'.venv/bin/python'),
    [path.join(__dirname,'qa_capacity_resilience_http.py'),'--output',output],{cwd:root,
      env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[path.join(root,'src'),path.join(core,'src')].join(path.delimiter)}});
  let browser,page;
  const inspect=async(command={command:'inspect'})=>{fixture.send(command);return fixture.next();};
  const save=async name=>{
    await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});report.screenshots.push(name+'.png');
    await fs.writeFile(path.join(output,name+'.aria.txt'),await page.locator('body').ariaSnapshot());
  };
  try {
    report.source_before={monitor:await capture(root,owned),core:await capture(core)};
    const ready=await fixture.next();assert.equal(ready.type,'ready');
    browser=await chromium.launch({channel:'chrome',headless:process.env.CLOUDYFAN_QA_HEADED!=='1'});
    report.browser=browser.version();
    const context=await browser.newContext({viewport:report.viewport,locale:'zh-TW',serviceWorkers:'block'});
    await context.route('**/*',route=>{
      if(new URL(route.request().url()).origin===ready.origin)return route.continue();
      report.blocked_browser_requests.push(new URL(route.request().url()).origin);return route.abort();
    });
    page=await context.newPage();page.setDefaultTimeout(6000);
    page.on('pageerror',()=>report.page_errors.push('JavaScript page error'));
    page.on('console',message=>{if(message.type()==='error')report.console_errors.push(message.text());});
    page.on('response',response=>report.http_responses.push({method:response.request().method(),
      path:new URL(response.url()).pathname,status:response.status()}));
    await page.goto(ready.origin);await page.locator('#environment').filter({hasText:/模擬/}).waitFor();
    await page.locator('#logout').click();await page.locator('#token').fill(ready.operator_token);
    await page.getByRole('button',{name:'登入',exact:true}).click();
    await page.locator('#login[open]').waitFor({state:'hidden'});
    await page.locator('a[data-page="capacity"]').click();
    await page.locator('#capacity-policy-version').selectOption(ready.policy_version);
    await page.locator('#capacity-activate-policy').click();await page.locator('#confirm[open]').waitFor();
    const active=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity/activate')&&response.request().method()==='POST');
    await page.locator('#apply-confirm').click();assert.equal((await active).status(),200);
    await page.locator('#capacity-active-version').filter({hasText:ready.policy_version}).waitFor();
    report.setup_assertions.push('actual_operator_activation');
    const operator={Authorization:'Bearer '+ready.operator_token},client={Authorization:'Bearer '+ready.credentials.studio};
    const accepted=await context.request.post(ready.origin+'/v1/tasks',{headers:client,
      data:{run_id:'resilience-original-window',category:ready.category,prompt:'Return only 1+1. PRIVATE_RESILIENCE_PROMPT_CANARY'}});
    assert.equal(accepted.status(),202);report.setup_assertions.push('actual_client_queue_acceptance');
    await inspect({command:'work'});
    const deadline=Date.now()+15000;
    for(;;) {
      const current=await inspect();
      if(!current.worker_running)break;
      assert.ok(Date.now()<deadline,'Actual fixture worker did not complete its controlled response');
      await page.waitForTimeout(25);
    }
    await inspect({command:'advance',seconds:61});
    let response=await context.request.get(ready.origin+'/monitor/capacity',{headers:operator});
    assert.equal(response.status(),200);
    const before=await response.json(),facts=await inspect();
    assert.equal(before.dispatch.integrity.protocol,'capacity-preservation-v1');
    assert.equal(before.dispatch.integrity.status,'verified');
    assert.ok(before.dispatch.windows.groups.some(row=>row.status==='wait'&&row.not_before>facts.clock));
    assert.equal(facts.model_requests.length,1);assert.ok(Object.values(facts.budget).every(row=>row.unknown_events===0));
    assert.ok(Object.values(facts.budget).some(row=>Number(row.known_usd)>0));
    report.setup_assertions.push('real_known_fee_original_long_window_survives_rate_horizon');
    await fs.writeFile(path.join(output,'safe-before-fault-capacity.json'),JSON.stringify(before,null,2));
    await inspect({command:'fault_missing_window_table'});
    response=await context.request.get(ready.origin+'/monitor/capacity',{headers:operator});
    assert.equal(response.status(),200);
    const after=await response.json(),observed=await inspect();
    assert.equal(after.dispatch.integrity.status,'unavailable');
    assert.equal(after.dispatch.integrity.automatic_repair,false);
    assert.equal(observed.model_requests.length,facts.model_requests.length);
    assert.deepEqual(observed.budget,facts.budget);
    assert.deepEqual(observed.original_evidence_sha256,facts.original_evidence_sha256);
    assert.deepEqual(observed.source_requests,[]);assert.deepEqual(observed.accounting_requests,[]);
    assert.ok(!JSON.stringify(after).includes('PRIVATE_'));
    report.setup_assertions.push('real_new_copy_schema_loss_is_unavailable_without_call_or_fee_change');
    report.backend_integrity=after.dispatch.integrity;
    await fs.writeFile(path.join(output,'safe-after-fault-capacity.json'),JSON.stringify(after,null,2));
    await page.evaluate(()=>window.CloudyFanCapacity.refresh(true));
    await page.getByRole('heading',{name:'容量狀態無法核對',exact:true}).waitFor();
    assert.match(await page.locator('#capacity-integrity').innerText(),/原保存資料/);
    assert.match(await page.locator('#capacity-integrity').innerText(),/不能.*可用|不代表.*可用/);
    assert.ok((await page.locator('[data-capacity-counter]').allTextContents()).every(value=>value==='未知'));
    assert.equal(await page.locator('#capacity-policy-version').isDisabled(),true);
    assert.equal(await page.locator('#capacity-refresh-sources').isDisabled(),true);
    assert.match(await page.locator('#capacity-dispatch-status').innerText(),/原.*待核對/);
    await page.getByText('原保存政策版本',{exact:true}).waitFor();
    const preserved=page.locator('[data-capacity-detail="preserved-usage:shared"]');
    await preserved.locator('summary').click();
    assert.ok((await preserved.locator('[data-capacity-original-counter]').allTextContents()).includes('0'));
    assert.ok(!(await page.locator('#main').innerText()).includes('PRIVATE_'));
    await save('01-preserved-window-current-store-unavailable');
    await page.locator('#capacity-integrity').screenshot({path:path.join(output,'01-current-integrity-detail.png')});
    report.screenshots.push('01-current-integrity-detail.png');
    await page.locator('#capacity-dispatch').screenshot({path:path.join(output,'01-preserved-policy-counter-detail.png')});
    report.screenshots.push('01-preserved-policy-counter-detail.png');
    report.checks.push({name:'actual_schema_loss_distinguishes_saved_facts_from_current_unavailable_not_free',passed:true});
    assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.blocked_browser_requests,[]);
    report.status='passed';
  } catch(error) {
    report.status='failed';report.failure={name:error.name,message:error.message,stack:error.stack};process.exitCode=1;
    if(page)await save('failure').catch(()=>{});
  } finally {
    if(browser)await browser.close();
    report.fixture_stop=await fixture.stop();
    if(report.fixture_stop.code!==0||report.fixture_stop.cleanup_failed){report.status='failed';process.exitCode=1;}
    if(report.source_before) {
      report.source_after={monitor:await capture(root,owned),core:await capture(core)};
      report.source_inputs_unchanged=JSON.stringify(report.source_before)===JSON.stringify(report.source_after);
      if(!report.source_inputs_unchanged){report.status='failed';process.exitCode=1;}
    }
    await fs.writeFile(path.join(output,'desktop-receipt.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({status:report.status,checks:report.checks.length,
      setup_assertions:report.setup_assertions.length,output}));
  }
}
main().catch(error=>{process.stderr.write(error.name+': private desktop setup failed\n');process.exitCode=1;});
