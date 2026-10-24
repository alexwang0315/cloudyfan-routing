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
  const output=process.argv[2]?path.resolve(process.argv[2]):path.join(root,'runtime/capacity-admission-desktop',randomUUID().slice(0,12));
  await fs.mkdir(path.dirname(output),{recursive:true});await fs.mkdir(output,{recursive:false,mode:0o700});
  const report={schema_version:1,status:'incomplete',viewport:{width:1280,height:800},checks:[],screenshots:[],
    http_responses:[],page_errors:[],console_errors:[],expected_http_errors:[],blocked_browser_requests:[],
    scope:'actual Chrome + loopback HTTP; real synthetic controlled host; MockTransport only; no supplier qualification or SLO claim'};
  const fixture=startFixture(process.env.CLOUDYFAN_QA_PYTHON||path.join(core,'.venv/bin/python'),
    [path.join(__dirname,'qa_capacity_admission_http.py'),'--output',output],{cwd:root,
      env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[path.join(root,'src'),path.join(core,'src')].join(path.delimiter)}});
  let browser,page,activeTask;
  const inspect=async(command={command:'inspect'})=>{fixture.send(command);return fixture.next();};
  const step=async(name,action)=>{await action();report.checks.push({name,passed:true});console.log('PASS '+name);};
  const shot=async name=>{await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});report.screenshots.push(name+'.png');
    await fs.writeFile(path.join(output,name+'.aria.txt'),await page.locator('body').ariaSnapshot());};
  try {
    const names=['src/cloudyfan_monitor/static/capacity.js','src/cloudyfan_monitor/static/capacity.css','src/cloudyfan_monitor/app.py',
      'src/cloudyfan_monitor/integration.py','tests/capacity_admission_fixture.py','scripts/qa_capacity_admission_http.py','scripts/qa_capacity_admission_desktop.cjs'];
    report.source_sha256=Object.fromEntries(await Promise.all(names.map(async name=>[name,createHash('sha256').update(await fs.readFile(path.join(root,name))).digest('hex')])));
    const coreNames=['capacity_dispatch.py','capacity.py','client.py','budget.py','http_api.py','engine.py','local.py'];
    report.core_source_sha256=Object.fromEntries(await Promise.all(coreNames.map(async name=>[name,
      createHash('sha256').update(await fs.readFile(path.join(core,'src/cloudyfan_jev',name))).digest('hex')])));
    const ready=await fixture.next();assert.equal(ready.type,'ready');
    browser=await chromium.launch({channel:'chrome',headless:true});report.browser=browser.version();
    const context=await browser.newContext({viewport:report.viewport,locale:'zh-TW',serviceWorkers:'block'});
    await context.route('**/*',route=>{if(new URL(route.request().url()).origin===ready.origin)return route.continue();
      report.blocked_browser_requests.push(new URL(route.request().url()).origin);return route.abort();});
    page=await context.newPage();page.setDefaultTimeout(6000);
    page.on('pageerror',()=>report.page_errors.push('JavaScript page error'));
    page.on('console',message=>{if(message.type()==='error')report.console_errors.push(message.text());});
    page.on('response',response=>report.http_responses.push({method:response.request().method(),path:new URL(response.url()).pathname,status:response.status()}));
    await page.goto(ready.origin);await page.locator('#environment').filter({hasText:/模擬/}).waitFor();
    await page.locator('#logout').click();await page.locator('#token').fill(ready.operator_token);await page.getByRole('button',{name:'登入',exact:true}).click();
    await page.locator('#login[open]').waitFor({state:'hidden'});await page.locator('a[data-page="capacity"]').click();
    await step('operator_sees_only_registered_capacity_policies_and_original_disabled_semantics',async()=>{
      const selector=page.locator('#capacity-policy-version');await selector.waitFor();
      assert.deepEqual(await selector.locator('option').evaluateAll(nodes=>nodes.map(node=>node.value)),['','synthetic-admission-v1','synthetic-admission-v2']);
      assert.match(await page.locator('#capacity-dispatch').innerText(),/尚未啟用/);
      assert.equal((await inspect()).model_requests.length,0);await shot('01-registered-policy-disabled');
    });
    await step('explicit_policy_activation_and_held_request_show_separate_capacity_counts_before_release',async()=>{
      await page.locator('#capacity-policy-version').selectOption('synthetic-admission-v1');
      await page.locator('#capacity-activate-policy').click();await page.locator('#confirm[open]').waitFor();
      assert.equal((await inspect()).model_requests.length,0);
      const applied=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity/activate'));
      await page.locator('#apply-confirm').click();assert.equal((await applied).status(),200);
      await page.locator('#confirm[open]').waitFor({state:'hidden'});
      assert.equal(await page.locator('#capacity-active-version').innerText(),'synthetic-admission-v1');
      await inspect({command:'hold'});
      activeTask=context.request.post(ready.origin+'/v1/tasks',{headers:{Authorization:'Bearer '+ready.credentials.studio},
        data:{run_id:'capacity-desktop-one',category:ready.category,prompt:'Return only 1+1. PRIVATE_CAPACITY_PROMPT_MARKER'}});
      for(let attempts=0;attempts<30;attempts++) {if((await inspect()).model_response_held)break;await page.waitForTimeout(25);}
      const before=await inspect();assert.equal(before.model_response_held,true);
      const refreshed=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity')&&response.request().method()==='GET');
      await page.locator('#reload').click();await (await refreshed).finished();
      const group=page.locator('[data-capacity-usage="shared"]');await group.waitFor();
      assert.equal(await group.locator('[data-capacity-counter="rpm"]').innerText(),'1');
      assert.equal(await group.locator('[data-capacity-counter="tpm"]').innerText(),'1000');
      assert.equal(await group.locator('[data-capacity-counter="concurrency"]').innerText(),'1');
      const after=await inspect();assert.equal(after.model_response_held,true);
      assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      assert.equal(after.capacity_sha256,before.capacity_sha256);assert.equal(after.health_sha256,before.health_sha256);
      assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
      await shot('02-active-capacity-held');await inspect({command:'release'});
      const completed=await activeTask;activeTask=null;assert.equal(completed.status(),200);assert.equal((await completed.json()).status,'passed');
    });
    await step('actual_capacity_refusal_and_original_policy_reason_remain_readonly_in_admission_history',async()=>{
      const clientHeaders={Authorization:'Bearer '+ready.credentials.agent};
      const task={run_id:'capacity-desktop-refused',category:ready.category,prompt:'Return only 1+1. PRIVATE_CAPACITY_OTHER_PROMPT'};
      const denied=await context.request.post(ready.origin+'/v1/tasks',{headers:clientHeaders,data:task});
      assert.equal(denied.status(),429);const outcome=await denied.json();
      assert.equal(outcome.status,'capacity_denied');assert.equal(outcome.error.code,'capacity_rpm_exhausted');
      assert.equal(outcome.costs.unknown_events,0);
      const refresh=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity')&&response.request().method()==='GET');
      await page.locator('#reload').click();await (await refresh).finished();
      const history=page.locator('#capacity-admission-history');await history.waitFor();
      await history.locator('summary').focus();await page.keyboard.press('Enter');
      const refused=history.locator('[data-capacity-event="refused"]');await refused.waitFor();
      assert.match(await refused.innerText(),/capacity_rpm_exhausted/);assert.match(await refused.innerText(),/synthetic-admission-v1/);
      const before=await inspect();
      for(let attempt=0;attempt<2;attempt++) {
        const replay=await context.request.post(ready.origin+'/v1/tasks',{headers:clientHeaders,data:task});
        assert.equal(replay.status(),429);assert.deepEqual(await replay.json(),outcome);
        const resume=await context.request.post(ready.origin+'/v1/tasks/'+task.run_id+'/resume',{headers:clientHeaders,data:{}});
        assert.equal(resume.status(),429);assert.deepEqual(await resume.json(),outcome);
      }
      const poll=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity')&&response.request().method()==='GET');
      await history.locator('summary').focus();await (await poll).finished();
      await page.waitForFunction(()=>document.activeElement?.closest('#capacity-admission-history')?.open===true);
      const after=await inspect();assert.equal(after.model_requests.length,1);assert.deepEqual(after.model_requests,before.model_requests);
      assert.deepEqual(after.budget,before.budget);assert.equal(after.capacity_sha256,before.capacity_sha256);
      assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
      assert.ok(!(await page.locator('#capacity-page').innerText()).includes('PRIVATE_'));
      await shot('03-frozen-refusal-history');
    });
    await step('operator_handles_stale_revision_and_live_window_refusals_without_retry_or_policy_change',async()=>{
      const before=await inspect();
      await page.locator('#capacity-policy-version').selectOption('synthetic-admission-v2');
      await page.locator('#capacity-activate-policy').click();await page.locator('#confirm[open]').waitFor();
      const revision=Number(await page.locator('#capacity-policy-revision').innerText());
      const concurrent=await context.request.post(ready.origin+'/monitor/capacity/activate',{
        headers:{Authorization:'Bearer '+ready.operator_token},data:{revision,version:'synthetic-admission-v1'}});
      assert.equal(concurrent.status(),200);
      const stale=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity/activate')&&response.status()===409);
      await page.locator('#apply-confirm').click();const staleResponse=await stale;assert.equal(staleResponse.status(),409);
      assert.equal((await staleResponse.json()).error.code,'capacity_revision_conflict');
      report.expected_http_errors.push({method:'POST',path:'/monitor/capacity/activate',status:409,code:'capacity_revision_conflict'});
      await page.locator('#confirm[open]').waitFor({state:'hidden'});
      await page.locator('#capacity-activation-error').filter({hasText:/已由另一個操作更新/}).waitFor();
      assert.equal(await page.locator('#capacity-active-version').innerText(),'synthetic-admission-v1');
      await page.locator('#capacity-policy-version').selectOption('');await page.locator('#capacity-activate-policy').click();
      const blocked=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity/activate')&&response.status()===409);
      await page.locator('#apply-confirm').click();const blockedResponse=await blocked;assert.equal(blockedResponse.status(),409);
      assert.equal((await blockedResponse.json()).error.code,'capacity_live_state_blocks_change');
      report.expected_http_errors.push({method:'POST',path:'/monitor/capacity/activate',status:409,code:'capacity_live_state_blocks_change'});
      await page.locator('#confirm[open]').waitFor({state:'hidden'});
      await page.locator('#capacity-activation-error').filter({hasText:/未決名額或有效窗口用量/}).waitFor();
      assert.equal(await page.locator('#capacity-active-version').innerText(),'synthetic-admission-v1');
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      await shot('04-live-window-switch-refused');
      await page.locator('#capacity-policy-version').selectOption('synthetic-admission-v2');await page.locator('#capacity-policy-version').focus();
      const poll=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity')&&response.request().method()==='GET');
      await (await poll).finished();await page.waitForFunction(()=>document.activeElement?.id==='capacity-policy-version');
      assert.equal(await page.locator('#capacity-policy-version').inputValue(),'synthetic-admission-v2');
      const dispatch=await context.request.get(ready.origin+'/monitor/capacity',{headers:{Authorization:'Bearer '+ready.operator_token}});
      const view=(await dispatch.json()).dispatch;
      assert.equal(view.active_version,'synthetic-admission-v1');
      assert.ok(view.allocations.some(item=>item.wire_sha256===after.model_requests[0].wire_sha256));
    });
    await step('failed_read_keeps_original_evidence_and_moves_disabled_selector_focus_to_reload',async()=>{
      const before=await inspect();await page.locator('#capacity-policy-version').focus();
      const failed=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity')&&response.status()===503);
      await inspect({command:'fail_read'});await (await failed).finished();
      report.expected_http_errors.push({method:'GET',path:'/monitor/capacity',status:503,code:'controlled_read_fault'});
      await page.locator('#capacity-page').getByText('本次讀取失敗，保留上次畫面與原始時間。',{exact:true}).waitFor();
      assert.equal(await page.locator('#capacity-policy-version').isDisabled(),true);
      await page.waitForFunction(()=>document.activeElement?.id==='reload');
      assert.equal(await page.locator('#capacity-active-version').innerText(),'synthetic-admission-v1');
      const after=await inspect();assert.equal(after.capacity_sha256,before.capacity_sha256);
      assert.deepEqual(after.budget,before.budget);assert.deepEqual(after.model_requests,before.model_requests);
      assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
      await shot('05-stale-read-focus-fallback');await inspect({command:'restore_read'});
      const recovered=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity')&&response.status()===200);
      await page.locator('#reload').click();await (await recovered).finished();
      assert.equal(await page.locator('#capacity-policy-version').isDisabled(),false);
    });
    await step('client_scope_revocation_and_operator_login_keep_capacity_history_private',async()=>{
      for(const principal of ['studio','agent']) {
        const headers={Authorization:'Bearer '+ready.credentials[principal]};
        const forbidden=await context.request.get(ready.origin+'/monitor/capacity',{headers});assert.equal(forbidden.status(),401);
        const own=await context.request.get(ready.origin+'/v1/monitor',{headers});assert.equal(own.status(),200);
        const ownView=JSON.stringify(await own.json());assert.ok(!ownView.includes('registered_policies'));
        assert.ok(!ownView.includes('capacity_allocations'));assert.ok(!ownView.includes('PRIVATE_'));
      }
      const cross=await context.request.get(ready.origin+'/v1/tasks/capacity-desktop-refused',{
        headers:{Authorization:'Bearer '+ready.credentials.studio}});assert.equal(cross.status(),404);
      await inspect({command:'revoke_agent'});
      const revoked=await context.request.get(ready.origin+'/v1/tasks/capacity-desktop-refused',{
        headers:{Authorization:'Bearer '+ready.credentials.agent}});assert.equal(revoked.status(),401);
      const before=await inspect();
      await page.locator('#logout').click();await page.locator('#token').fill(ready.credentials.agent);
      await page.getByRole('button',{name:'登入',exact:true}).click();
      await page.locator('#login-error').filter({hasText:/登入憑證不正確或已失效/}).waitFor();
      report.expected_http_errors.push({method:'GET',path:'/monitor/snapshot',status:401,code:'unauthorized'});
      assert.equal(await page.locator('#capacity-dispatch').count(),0);
      await page.locator('#token').fill(ready.operator_token);await page.getByRole('button',{name:'登入',exact:true}).click();
      await page.locator('#login[open]').waitFor({state:'hidden'});await page.locator('#capacity-dispatch').waitFor();
      assert.ok(!(await page.locator('#main').innerText()).includes('PRIVATE_'));
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      assert.equal(after.capacity_sha256,before.capacity_sha256);assert.equal(after.health_sha256,before.health_sha256);
      assert.deepEqual(after.source_requests,[]);assert.deepEqual(after.accounting_requests,[]);
      await shot('05-private-operator-history');
    });
    await step('explicit_disable_after_original_window_keeps_frozen_task_and_refusal_history',async()=>{
      await inspect({command:'advance',seconds:61});
      const before=await inspect();
      await page.locator('#capacity-policy-version').selectOption('');await page.locator('#capacity-activate-policy').click();
      await page.locator('#confirm[open]').waitFor();
      const changed=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity/activate'));
      await page.locator('#apply-confirm').click();assert.equal((await changed).status(),200);
      await page.locator('#confirm[open]').waitFor({state:'hidden'});
      assert.equal(await page.locator('#capacity-active-version').innerText(),'未啟用');
      let own;
      for(let attempt=0;attempt<20;attempt++) {
        const response=await context.request.get(ready.origin+'/v1/tasks/capacity-desktop-one',{
          headers:{Authorization:'Bearer '+ready.credentials.studio}});assert.equal(response.status(),200);
        own=await response.json();if(own.observation!=='busy')break;await page.waitForTimeout(25);
      }
      assert.equal(own.capacity.policy_version,'synthetic-admission-v1');
      const history=page.locator('#capacity-admission-history');await history.locator('summary').focus();await page.keyboard.press('Enter');
      assert.match(await history.locator('[data-capacity-event="refused"]').innerText(),/synthetic-admission-v1/);
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);assert.deepEqual(after.blocked_server_socket_attempts,[]);
      await shot('06-disabled-frozen-history');
    });
    report.console_classification=report.console_errors.map(message=>({message,
      classification:/status of (409|401|503)\b/.test(message)&&report.expected_http_errors.some(item=>message.includes(String(item.status)))?
        'expected_server_conflict_unauthorized_or_controlled_read_fault':'unexpected'}));
    assert.ok(report.console_classification.every(item=>item.classification!=='unexpected'));
    assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.blocked_browser_requests,[]);report.status='passed';
  } catch(error) {
    report.status='failed';report.failure={name:error.name,message:error.message,stack:error.stack};process.exitCode=1;
    if(page)await shot('failure').catch(()=>{});
  } finally {
    if(activeTask){await inspect({command:'release'}).catch(()=>{});await activeTask.catch(()=>{});}
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
