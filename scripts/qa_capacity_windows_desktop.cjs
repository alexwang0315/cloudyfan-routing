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
  let browser,page,activeTask;
  const inspect=async(command={command:'inspect'})=>{fixture.send(command);return fixture.next();};
  const step=async(name,action)=>{await action();report.checks.push({name,passed:true});console.log('PASS '+name);};
  const shot=async name=>{await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});report.screenshots.push(name+'.png');
    await fs.writeFile(path.join(output,name+'.aria.txt'),await page.locator('body').ariaSnapshot());};
  const unchanged=(before,after)=>{
    for(const key of ['model_requests','source_requests','accounting_requests','budget','capacity_sha256','health_sha256','original_evidence_sha256'])
      assert.deepEqual(after[key],before[key],key+' changed during read-only checks');
  };
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
    const disabled=await context.request.get(ready.origin+'/monitor/capacity',{headers:operator});assert.equal(disabled.status(),200);
    const disabledView=await disabled.json();assert.equal(disabledView.dispatch.active_version,null);assert.equal(disabledView.execution_enabled,false);
    assert.equal((await inspect()).model_requests.length,0);
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
      assert.match(await panel.innerText(),/新任務已採用登錄窗口版本/);
      await panel.locator('[data-capacity-window]').first().locator('summary').click();
      assert.match(await panel.innerText(),/retry-after/);
      await shot('01-original-response-window');
      await panel.screenshot({path:path.join(output,'01-window-panel-detail.png')});report.screenshots.push('01-window-panel-detail.png');
    });
    await step('saved_prepost_exclusions_and_actual_availability_fallback_are_distinct_from_quality_repair',async()=>{
      const before=await inspect();
      let response=await context.request.post(ready.origin+'/v1/tasks',{headers:client,data:task('window-prepost-exclusion')});
      assert.equal(response.status(),200);assert.equal((await response.json()).status,'passed');
      assert.equal((await inspect()).model_requests.length,before.model_requests.length+1);
      await inspect({command:'scenario',name:'quality'});
      response=await context.request.post(ready.origin+'/v1/tasks',{headers:client,data:task('window-quality-repair')});
      assert.equal(response.status(),200);assert.equal((await response.json()).status,'passed');
      assert.equal((await inspect()).model_requests.length,before.model_requests.length+3);
      const snapshot=await context.request.get(ready.origin+'/monitor/snapshot',{headers:operator});assert.equal(snapshot.status(),200);
      await fs.writeFile(path.join(output,'safe-original-task-snapshot.json'),JSON.stringify(await snapshot.json(),null,2));
      const loaded=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity')&&response.request().method()==='GET');
      await page.locator('#reload').click();await (await loaded).finished();
      const panel=page.locator('#capacity-window-decisions');await panel.waitFor();
      const fallback=panel.locator('[data-window-task="studio:window-fallback"]');
      await fallback.locator('summary').first().click();
      assert.match(await fallback.locator('[data-window-transition="availability_fallback"]').innerText(),/可用性備援/);
      const quality=panel.locator('[data-window-task="studio:window-quality-repair"]');
      await quality.locator('summary').first().click();
      assert.match(await quality.locator('[data-window-transition="quality_recovery"]').innerText(),/公開驗收.*品質補救/);
      const excluded=panel.locator('[data-window-task="studio:window-prepost-exclusion"]');
      await excluded.locator('summary').first().click();
      assert.match(await excluded.innerText(),/派送前排除/);
      await shot('02-candidate-exclusion-and-distinct-reasons');
    });
    await step('live_window_and_stale_revision_conflicts_are_honest_without_retry_or_policy_override',async()=>{
      const before=await inspect();
      await page.locator('#capacity-policy-version').selectOption('synthetic-window-admission-v2');
      await page.locator('#capacity-activate-policy').click();await page.locator('#confirm[open]').waitFor();
      const revision=Number(await page.locator('#capacity-policy-revision').innerText());
      const concurrent=await context.request.post(ready.origin+'/monitor/capacity/activate',{headers:operator,
        data:{revision,version:'synthetic-window-admission-v1'}});assert.equal(concurrent.status(),200);
      let pending=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity/activate')&&response.status()===409);
      await page.locator('#apply-confirm').click();let failed=await pending;
      assert.equal((await failed.json()).error.code,'capacity_revision_conflict');
      report.expected_http_errors.push({method:'POST',path:'/monitor/capacity/activate',status:409,code:'capacity_revision_conflict'});
      await page.locator('#confirm[open]').waitFor({state:'hidden'});
      await page.locator('#capacity-activation-error').filter({hasText:/已由另一個操作更新/}).waitFor();
      await page.locator('#capacity-policy-version').selectOption('');await page.locator('#capacity-activate-policy').click();
      await page.locator('#confirm[open]').waitFor();
      pending=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity/activate')&&response.status()===409);
      await page.locator('#apply-confirm').click();failed=await pending;
      assert.equal((await failed.json()).error.code,'capacity_live_state_blocks_change');
      report.expected_http_errors.push({method:'POST',path:'/monitor/capacity/activate',status:409,code:'capacity_live_state_blocks_change'});
      await page.locator('#confirm[open]').waitFor({state:'hidden'});
      await page.locator('#capacity-activation-error').filter({hasText:/有效窗口用量/}).waitFor();
      assert.equal(await page.locator('#capacity-active-version').innerText(),'synthetic-window-admission-v1');
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      await shot('03-window-blocks-override');
    });
    await step('held_model_reads_and_polling_keep_window_disclosure_focus_and_all_original_evidence',async()=>{
      await inspect({command:'hold'});
      activeTask=context.request.post(ready.origin+'/v1/tasks',{headers:headers(ready.credentials.agent),data:task('window-held-agent')});
      for(let attempt=0;attempt<30;attempt++){if((await inspect()).model_response_held)break;await page.waitForTimeout(25);}
      const before=await inspect();assert.equal(before.model_response_held,true);
      const row=page.locator('#capacity-windows > .capacity-window-grid > [data-capacity-window]').first();
      if(!await row.evaluate(node=>node.open))await row.locator('summary').click();
      const id=await row.getAttribute('data-capacity-window');
      const times=await row.locator('[data-window-time]').allTextContents();
      await row.locator('summary').focus();
      const poll=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity')&&response.request().method()==='GET',{timeout:10000});
      await (await poll).finished();
      await page.waitForFunction(id=>document.activeElement?.closest('[data-capacity-detail="window:'+id+'"]')?.open===true,id);
      const after=await inspect();assert.equal(after.model_response_held,true);unchanged(before,after);
      assert.deepEqual(await page.locator('[data-capacity-detail="window:'+id+'"]').locator('[data-window-time]').allTextContents(),times);
      assert.ok(!(await page.locator('#main').innerText()).includes('PRIVATE_'));
      await shot('04-held-read-and-keyboard-focus');
      await inspect({command:'release'});const completed=await activeTask;activeTask=null;
      assert.equal(completed.status(),200);assert.equal((await completed.json()).status,'passed');
    });
    await step('late_real_capacity_response_does_not_replace_another_page_context',async()=>{
      const before=await inspect();let release,entered;
      const gate=new Promise(resolve=>release=resolve),seen=new Promise(resolve=>entered=resolve);let once=true;
      const handler=async route=>{if(!once)return route.continue();once=false;const response=await route.fetch();entered();await gate;await route.fulfill({response});};
      await page.route('**/monitor/capacity',handler);
      const refresh=page.evaluate(()=>window.CloudyFanCapacity.refresh(true));await seen;
      await page.locator('a[data-page="tasks"]').click();await page.locator('#capacity-page').waitFor({state:'detached'});
      release();await refresh;assert.equal(await page.locator('#capacity-page').count(),0);
      await page.unroute('**/monitor/capacity',handler);unchanged(before,await inspect());
      await page.locator('a[data-page="capacity"]').click();await page.locator('#capacity-windows').waitFor();
    });
    await step('window_expiry_removes_only_current_time_gate_keeps_original_history_and_missing_focus_falls_back',async()=>{
      const before=await inspect(),original=before.window_snapshot.events[0];
      const row=page.locator('[data-capacity-detail="window:'+original.id+'"]');
      if(!await row.evaluate(node=>node.open))await row.locator('summary').click();
      await row.locator('summary').focus();await inspect({command:'advance',seconds:30});
      const poll=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity')&&response.request().method()==='GET',{timeout:10000});
      await (await poll).finished();await page.waitForFunction(()=>document.activeElement?.id==='reload');
      assert.equal(await page.locator('[data-capacity-detail="window:'+original.id+'"]').count(),0);
      const after=await inspect();unchanged(before,after);
      assert.deepEqual(after.window_snapshot.events.find(row=>row.id===original.id),original);
      const history=page.locator('#capacity-window-history');await history.locator('summary').first().click();
      await history.locator('[data-capacity-window="'+original.id+'"] > summary').click();
      assert.match(await history.innerText(),/原事件|原來源時間/);await shot('05-expired-original-history');
    });
    await step('late_authenticated_response_cannot_restore_an_expired_window_after_logout_and_new_login',async()=>{
      await inspect({command:'scenario',name:'window'});
      const taskResponse=await context.request.post(ready.origin+'/v1/tasks',{headers:client,data:task('window-auth-context')});
      assert.equal(taskResponse.status(),200);assert.equal((await taskResponse.json()).status,'passed');
      const state=await inspect(),oldId=state.window_snapshot.groups[0].id;
      let release,entered;const gate=new Promise(resolve=>release=resolve),seen=new Promise(resolve=>entered=resolve);let once=true;
      const handler=async route=>{if(!once)return route.continue();once=false;const response=await route.fetch();entered();await gate;await route.fulfill({response});};
      await page.route('**/monitor/capacity',handler);
      const refresh=page.evaluate(()=>window.CloudyFanCapacity.refresh(true));await seen;
      await inspect({command:'advance',seconds:30});
      await page.locator('#logout').click();await page.locator('#token').fill(ready.operator_token);await page.getByRole('button',{name:'登入',exact:true}).click();
      await page.locator('#login[open]').waitFor({state:'hidden'});await page.locator('#capacity-windows').waitFor();
      assert.equal(await page.locator('[data-capacity-detail="window:'+oldId+'"]').count(),0);
      release();await refresh;await page.unroute('**/monitor/capacity',handler);
      assert.equal(await page.locator('[data-capacity-detail="window:'+oldId+'"]').count(),0);
      const after=await inspect();assert.equal(after.model_requests.length,state.model_requests.length);
      assert.equal(after.capacity_sha256,state.capacity_sha256);assert.deepEqual(after.budget,state.budget);
      assert.deepEqual(after.window_snapshot.events,state.window_snapshot.events);
      await shot('06-new-auth-context-keeps-current-window-state');
    });
    await step('invalid_original_retry_after_is_explicit_unknown_and_does_not_clear_a_pause',async()=>{
      await inspect({command:'scenario',name:'unknown'});
      const response=await context.request.post(ready.origin+'/v1/tasks',{headers:client,data:task('window-invalid-time')});
      assert.equal(response.status(),200);assert.equal((await response.json()).status,'passed');
      await page.locator('#reload').click();
      const row=page.locator('#capacity-windows > .capacity-window-grid > [data-capacity-window]').first();await row.waitFor();
      await row.locator('summary').click();
      assert.match(await row.innerText(),/未知/);assert.match(await row.innerText(),/capacity_window_(header_invalid|clock_reference_unknown|time_invalid)/);
      assert.equal(await row.locator('[data-window-time="not-before"]').innerText(),'未知');
      const before=await inspect();assert.ok(before.window_snapshot.groups.some(row=>row.status==='unknown'&&row.not_before===null));
      for(let i=0;i<2;i++){await context.request.get(ready.origin+'/monitor/capacity',{headers:operator});await context.request.get(ready.origin+'/monitor/snapshot',{headers:operator});}
      unchanged(before,await inspect());await shot('07-invalid-time-remains-unknown');
    });
    await step('failed_read_preserves_original_window_and_moves_disabled_control_focus_to_reload',async()=>{
      const before=await inspect();await page.locator('#capacity-policy-version').focus();
      await inspect({command:'fail_read'});
      const failed=page.waitForResponse(response=>response.url().endsWith('/monitor/capacity')&&response.status()===503,{timeout:10000});
      await (await failed).finished();
      report.expected_http_errors.push({method:'GET',path:'/monitor/capacity',status:503,code:'controlled_read_fault'});
      await page.locator('#capacity-page').getByText('本次讀取失敗，保留上次畫面與原始時間。',{exact:true}).waitFor();
      assert.equal(await page.locator('#capacity-policy-version').isDisabled(),true);
      await page.waitForFunction(()=>document.activeElement?.id==='reload');unchanged(before,await inspect());
      await shot('08-stale-read-original-time');await inspect({command:'restore_read'});
      await page.locator('#reload').click();await page.waitForFunction(()=>!document.querySelector('#capacity-policy-version')?.disabled);
    });
    await step('operator_only_windows_own_task_isolation_revocation_and_private_canaries_hold',async()=>{
      const before=await inspect();
      for(const principal of ['studio','agent']) {
        const ownHeaders=headers(ready.credentials[principal]);
        const forbidden=await context.request.get(ready.origin+'/monitor/capacity',{headers:ownHeaders});assert.equal(forbidden.status(),401);
        const own=await context.request.get(ready.origin+'/v1/monitor',{headers:ownHeaders});assert.equal(own.status(),200);
        const safe=JSON.stringify(await own.json());assert.ok(!safe.includes('PRIVATE_'));assert.ok(!safe.includes('window_snapshot'));
        assert.ok(!safe.includes('synthetic-window-account-'));assert.ok(!safe.includes('registered_policies'));
      }
      const cross=await context.request.get(ready.origin+'/v1/tasks/window-fallback',{headers:headers(ready.credentials.agent)});assert.equal(cross.status(),404);
      await inspect({command:'revoke_agent'});
      const revoked=await context.request.get(ready.origin+'/v1/tasks/window-held-agent',{headers:headers(ready.credentials.agent)});assert.equal(revoked.status(),401);
      await page.locator('#logout').click();await page.locator('#token').fill(ready.credentials.agent);await page.getByRole('button',{name:'登入',exact:true}).click();
      await page.locator('#login-error').filter({hasText:/登入憑證不正確或已失效/}).waitFor();
      report.expected_http_errors.push({method:'GET',path:'/monitor/snapshot',status:401,code:'unauthorized'});
      assert.equal(await page.locator('#capacity-windows').count(),0);
      await page.locator('#token').fill(ready.operator_token);await page.getByRole('button',{name:'登入',exact:true}).click();
      await page.locator('#login[open]').waitFor({state:'hidden'});await page.locator('#capacity-windows').waitFor();
      assert.ok(!(await page.locator('#main').innerText()).includes('PRIVATE_'));unchanged(before,await inspect());
      assert.equal(await page.getByRole('button',{name:/retry|重試|探測|清除窗口|強制放行/i}).count(),0);
      await page.setViewportSize({width:700,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
      await shot('09-private-readonly-window-view-narrow');await page.setViewportSize(report.viewport);
    });
    await step('unknown_original_fee_stops_without_extra_generation_and_reads_do_not_reconcile',async()=>{
      const before=await inspect();await inspect({command:'scenario',name:'unknown_fee'});
      const response=await context.request.post(ready.origin+'/v1/tasks',{headers:client,data:task('window-unknown-fee')});
      const outcome=await response.json();assert.equal(outcome.status,'accounting_interrupted');
      const after=await inspect();
      assert.equal(after.model_requests.filter(row=>row.method==='POST').length,before.model_requests.filter(row=>row.method==='POST').length+1);
      assert.equal(after.model_requests.length,before.model_requests.length+1,'window-enabled unknown fee must not issue an automatic reconciliation GET');
      assert.ok(after.budget.unknown_events>0);
      await page.locator('#reload').click();
      await page.locator('[data-window-task="studio:window-unknown-fee"][data-window-task-status="accounting_interrupted"]').waitFor({timeout:10000});
      const frozen=await inspect();
      await context.request.get(ready.origin+'/monitor/capacity',{headers:operator});await context.request.get(ready.origin+'/monitor/snapshot',{headers:operator});
      const read=await inspect();unchanged(frozen,read);assert.deepEqual(read.source_requests,[]);assert.deepEqual(read.accounting_requests,[]);
      assert.deepEqual(read.blocked_server_socket_attempts,[]);
      const row=page.locator('[data-window-task="studio:window-unknown-fee"]');
      if(!await row.evaluate(node=>node.open))await row.locator('summary').click();
      assert.match(await row.innerText(),/費用待核/);await shot('10-unknown-fee-stops');
    });
    report.console_classification=report.console_errors.map(message=>({message,
      classification:/status of (409|401|503)\b/.test(message)&&report.expected_http_errors.some(item=>message.includes(String(item.status)))?
        'expected_server_conflict_unauthorized_or_controlled_read_fault':'unexpected'}));
    assert.ok(report.console_classification.every(item=>item.classification!=='unexpected'));
    assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.blocked_browser_requests,[]);
    report.status='passed';
  } catch(error) {
    report.status='failed';report.failure={name:error.name,message:error.message,stack:error.stack};process.exitCode=1;
    if(page)await shot('failure').catch(()=>{});
  } finally {
    if(activeTask){await inspect({command:'release'}).catch(()=>{});await activeTask.catch(()=>{});}
    if(browser)await browser.close();report.fixture_stop=await fixture.stop();
    if(report.fixture_stop.code!==0||report.fixture_stop.cleanup_failed){report.status='failed';process.exitCode=1;}
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
