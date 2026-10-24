#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {createHash,randomUUID} = require('node:crypto');
const {startFixture} = require('./qa_fixture_ipc.cjs');

async function main() {
  const root=path.resolve(__dirname,'..'),core=process.env.CLOUDYFAN_CORE_ROOT,windowScenario=process.argv.includes('--windows');
  assert.ok(core,'Use an explicitly matched Core root');
  const {chromium}=require(process.env.PLAYWRIGHT_PATH||'/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
  const output=process.argv[2]?path.resolve(process.argv[2]):path.join(root,'runtime/capacity-queue-desktop',randomUUID().slice(0,12));
  await fs.mkdir(path.dirname(output),{recursive:true});await fs.mkdir(output,{recursive:false,mode:0o700});
  const report={schema_version:1,status:'incomplete',scenario:windowScenario?'actual-response-window':'lifecycle',viewport:{width:1280,height:800},checks:[],screenshots:[],
    http_responses:[],page_errors:[],console_errors:[],expected_http_errors:[],blocked_browser_requests:[],
    scope:'actual Chrome + loopback HTTP; real synthetic controlled queue host; MockTransport only; no supplier qualification or SLO claim'};
  const fixture=startFixture(process.env.CLOUDYFAN_QA_PYTHON||path.join(core,'.venv/bin/python'),
    [path.join(__dirname,'qa_capacity_queue_http.py'),'--output',output,...(windowScenario?['--retry-after-seconds','30']:[])],{cwd:root,
      env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[path.join(root,'src'),path.join(core,'src')].join(path.delimiter)}});
  let browser,page;
  const inspect=async(command={command:'inspect'})=>{fixture.send(command);return fixture.next();};
  const step=async(name,action)=>{await action();report.checks.push({name,passed:true});console.log('PASS '+name);};
  const shot=async(name,{fullPage=true}={})=>{await page.screenshot({path:path.join(output,name+'.png'),fullPage});report.screenshots.push(name+'.png');
    await fs.writeFile(path.join(output,name+'.aria.txt'),await page.locator('body').ariaSnapshot());};
  const hash=async file=>createHash('sha256').update(await fs.readFile(file)).digest('hex');
  const sourceFiles=async(directory,prefix='')=>{
    const found=[];
    for(const entry of await fs.readdir(directory,{withFileTypes:true})) {
      const relative=prefix?prefix+'/'+entry.name:entry.name;
      if(entry.isDirectory()&&!entry.name.startsWith('.'))found.push(...await sourceFiles(path.join(directory,entry.name),relative));
      else if(entry.isFile()&&/\.(py|js|css|html)$/.test(entry.name))found.push(relative);
    }
    return found.sort();
  };
  const names=[...await sourceFiles(path.join(root,'src/cloudyfan_monitor'),'src/cloudyfan_monitor'),
    'tests/capacity_queue_fixture.py','tests/capacity_admission_fixture.py','tests/capacity_sources_fixture.py',
    'tests/test_sources.py','tests/test_local_http_integration.py',
    'scripts/qa_fixture_ipc.cjs','scripts/qa_capacity_queue_http.py','scripts/qa_capacity_queue_desktop.cjs'];
  const coreNames=await sourceFiles(path.join(core,'src/cloudyfan_jev'));
  try {
    report.source_sha256=Object.fromEntries(await Promise.all(names.map(async name=>[name,await hash(path.join(root,name))])));
    report.core_source_sha256=Object.fromEntries(await Promise.all(coreNames.map(async name=>[name,await hash(path.join(core,'src/cloudyfan_jev',name))])));
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
    await page.locator('#login[open]').waitFor({state:'hidden'});
    const activated=await context.request.post(ready.origin+'/monitor/capacity/activate',{
      headers:{Authorization:'Bearer '+ready.operator_token},data:{revision:0,version:ready.policy_version}});
    assert.equal(activated.status(),200);
    const task={run_id:'queue-desktop-one',category:ready.category,prompt:'Return only 1+1. PRIVATE_QUEUE_PROMPT_MARKER'};
    const headers={Authorization:'Bearer '+ready.credentials.studio};
    const accepted=await context.request.post(ready.origin+'/v1/tasks',{headers,data:task});
    assert.equal(accepted.status(),202);const original=await accepted.json();
    assert.equal(original.status,'queued');assert.equal(original.reason,'queue_waiting_worker');
    report.initial_acceptance={status:accepted.status(),run_id:original.run_id,status_value:original.status,reason:original.reason};
    await page.goto(ready.origin+'/#queue');
    if(windowScenario) {
      const waitWorker=async()=>{
        for(let attempt=0;attempt<80;attempt++) {const state=await inspect();if(!state.worker_running)return state;await page.waitForTimeout(25);}
        throw new Error('Controlled queue worker did not finish');
      };
      await step('saved_actual_429_with_known_fee_moves_next_dispatch_to_a_real_window_wait',async()=>{
        await page.locator('[data-queue-run="queue-desktop-one"]').waitFor();
        assert.equal((await inspect()).model_requests.length,0);
        await inspect({command:'advance',seconds:2});await inspect({command:'dispatch'});
        const first=await waitWorker();assert.equal(first.model_requests.length,1);
        assert.equal(Object.keys(first.original_evidence_sha256).filter(name=>name.endsWith('/response.json')).length,1);
        await inspect({command:'dispatch'});const waiting=await waitWorker();assert.equal(waiting.model_requests.length,1);
        assert.equal(waiting.worker_results.at(-1).claimed,false);
        const own=await context.request.get(ready.origin+'/v1/tasks/'+task.run_id,{headers});
        assert.equal(own.status(),202);const state=await own.json();
        assert.equal(state.reason,'queue_waiting_window');assert.equal(state.capacity_reason,'capacity_window_wait');
        assert.equal(state.costs.unknown_events,0);assert.notEqual(state.costs.known_api_usd,'0');
        assert.equal(state.queue_seconds,2);assert.equal(state.end_to_end_seconds,null);
        assert.equal(state.queue.deadline,original.queue.deadline);assert.deepEqual(state.queue.frozen,original.queue.frozen);
        const capacity=await context.request.get(ready.origin+'/monitor/capacity',{headers:{Authorization:'Bearer '+ready.operator_token}});
        assert.equal(capacity.status(),200);const window=(await capacity.json()).dispatch.windows;
        assert.equal(window.groups[0].not_before,original.queue.accepted_at+32);
        const read=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
        await page.locator('#reload').click();await (await read).finished();
        const job=page.locator('[data-queue-run="queue-desktop-one"]');
        await job.getByText('queue_waiting_window',{exact:true}).waitFor();
        await job.getByText('capacity_window_wait',{exact:true}).waitFor();
        assert.match(await job.innerText(),/等待適用的供應商窗口/);
        assert.ok(!(await job.innerText()).includes('queue_waiting_slot'));
        const detail=job.locator('details');await detail.locator('summary').focus();await page.keyboard.press('Enter');
        assert.equal(await detail.locator('[data-queue-frozen="windows-version"]').innerText(),original.queue.frozen.capacity.windows_version);
        assert.match(await detail.innerText(),/完整 API 費用\s+未知/);
        report.response_window={known_api_usd:state.costs.known_api_usd,not_before:window.groups[0].not_before,
          first_queue_seconds:state.queue_seconds,frozen_windows_version:original.queue.frozen.capacity.windows_version};
        await shot('14-actual-response-window-wait');
      });
      await step('ordinary_window_reads_and_original_resume_preserve_receipts_and_never_dispatch',async()=>{
        const before=await inspect();
        const polled=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
        await (await polled).finished();
        await page.waitForFunction(()=>document.activeElement?.closest('[data-queue-detail]')?.open===true);
        const replay=await context.request.post(ready.origin+'/v1/tasks',{headers,data:task});
        const resume=await context.request.post(ready.origin+'/v1/tasks/'+task.run_id+'/resume',{headers,data:{}});
        assert.equal(replay.status(),202);assert.equal(resume.status(),202);
        assert.equal((await replay.json()).reason,'queue_waiting_window');assert.equal((await resume.json()).reason,'queue_waiting_window');
        const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);
        assert.deepEqual(after.budget,before.budget);assert.equal(after.queue_sha256,before.queue_sha256);
        assert.equal(after.capacity_sha256,before.capacity_sha256);assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
        await inspect({command:'advance',seconds:29});await inspect({command:'dispatch'});
        const early=await waitWorker();assert.equal(early.worker_results.at(-1).claimed,false);assert.equal(early.model_requests.length,1);
        const still=await context.request.get(ready.origin+'/v1/tasks/'+task.run_id,{headers});
        assert.equal(still.status(),202);assert.equal((await still.json()).reason,'queue_waiting_window');
        report.response_window.early_dispatch_after_seconds=29;
        await shot('15-window-poll-keeps-original-fee');
      });
      await step('only_an_explicit_worker_after_the_saved_window_advances_original_classifier_and_answer',async()=>{
        await inspect({command:'advance',seconds:1});const before=await inspect();
        const read=await context.request.get(ready.origin+'/v1/tasks/'+task.run_id,{headers});
        assert.equal(read.status(),202);assert.equal((await read.json()).queue.deadline,original.queue.deadline);
        assert.deepEqual((await inspect()).model_requests,before.model_requests);
        await inspect({command:'dispatch'});const classifier=await waitWorker();assert.equal(classifier.model_requests.length,2);
        await inspect({command:'dispatch'});const final=await waitWorker();assert.equal(final.model_requests.length,3);
        const result=await context.request.get(ready.origin+'/v1/tasks/'+task.run_id,{headers});
        assert.equal(result.status(),200);const state=await result.json();assert.equal(state.status,'passed');
        assert.equal(state.queue_seconds,2);assert.equal(state.end_to_end_seconds,32);assert.equal(state.queue.waiting_seconds,32);
        assert.equal(state.queue.deadline,original.queue.deadline);assert.deepEqual(state.queue.frozen,original.queue.frozen);
        assert.equal(state.costs.unknown_events,0);assert.ok(state.costs.complete_api_usd);
        const saved=await inspect();
        const replay=await context.request.post(ready.origin+'/v1/tasks',{headers,data:task});
        assert.equal(replay.status(),200);assert.deepEqual(await replay.json(),state);
        const reload=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
        await page.locator('#reload').click();await (await reload).finished();
        const job=page.locator('[data-queue-run="queue-desktop-one"]');await job.getByText('通過',{exact:true}).waitFor();
        assert.equal(await job.locator('[data-queue-timing="waiting"]').innerText(),'32.00 秒');
        assert.equal(await job.locator('[data-queue-timing="end_to_end"]').innerText(),'32.00 秒');
        assert.ok(!(await page.locator('#queue-page').innerText()).includes('PRIVATE_'));
        const after=await inspect();assert.deepEqual(after.model_requests,saved.model_requests);assert.deepEqual(after.budget,saved.budget);
        assert.equal(after.queue_sha256,saved.queue_sha256);assert.equal(after.capacity_sha256,saved.capacity_sha256);
        assert.deepEqual(after.original_evidence_sha256,saved.original_evidence_sha256);
        report.response_window.final_post_count=3;report.response_window.end_to_end_seconds=state.end_to_end_seconds;
        await shot('16-window-original-completion');
      });
    } else {
    await step('real_202_queue_shows_fixed_limits_and_own_original_job_without_paid_dispatch',async()=>{
      await page.locator('#queue-page').waitFor();
      assert.equal(await page.locator('[data-queue-limit="capacity"]').innerText(),'4');
      assert.equal(await page.locator('[data-queue-limit="concurrency"]').innerText(),'1');
      assert.equal(await page.locator('[data-queue-limit="per_principal"]').innerText(),'2');
      assert.equal(await page.locator('[data-queue-limit="max_wait_seconds"]').innerText(),'120 秒');
      const job=page.locator('[data-queue-run="queue-desktop-one"]');await job.waitFor();
      assert.match(await job.innerText(),/queue_waiting_worker/);
      assert.match(await job.innerText(),/尚未完成/);
      const before=await inspect();assert.equal(before.model_requests.length,0);assert.equal(before.budget.reservations.length,0);
      assert.ok(!Object.keys(before.original_evidence_sha256).some(name=>name.endsWith('/intent.json')));
      const duplicate=await context.request.post(ready.origin+'/v1/tasks',{headers,data:task});
      assert.equal(duplicate.status(),202);assert.deepEqual(await duplicate.json(),original);
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      assert.equal(after.queue_sha256,before.queue_sha256);assert.equal(after.capacity_sha256,before.capacity_sha256);
      assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
      await shot('01-real-queued-limits');
    });
    await step('original_quota_and_queue_full_refusals_are_bounded_readonly_and_do_not_create_paid_work',async()=>{
      const studioExtra={...task,run_id:'queue-desktop-quota-held'};
      assert.equal((await context.request.post(ready.origin+'/v1/tasks',{headers,data:studioExtra})).status(),202);
      const quotaTask={...task,run_id:'queue-desktop-quota-refused'};
      const quota=await context.request.post(ready.origin+'/v1/tasks',{headers,data:quotaTask});
      assert.equal(quota.status(),429);assert.equal((await quota.json()).error.code,'principal_quota_full');
      const batchHeaders={Authorization:'Bearer '+ready.credentials.batch};
      const batchTasks=['queue-desktop-full-one','queue-desktop-full-two'].map(run_id=>({...task,run_id}));
      for(const next of batchTasks)assert.equal((await context.request.post(ready.origin+'/v1/tasks',{headers:batchHeaders,data:next})).status(),202);
      const fullTask={...task,run_id:'queue-desktop-full-refused'};
      const full=await context.request.post(ready.origin+'/v1/tasks',{headers:{Authorization:'Bearer '+ready.credentials.agent},data:fullTask});
      assert.equal(full.status(),429);assert.equal((await full.json()).error.code,'queue_full');
      const before=await inspect();assert.equal(before.model_requests.length,0);assert.deepEqual(before.budget.reservations,[]);
      const viewResponse=await context.request.get(ready.origin+'/monitor/queue',{headers:{Authorization:'Bearer '+ready.operator_token}});
      assert.equal(viewResponse.status(),200);const view=await viewResponse.json();
      assert.equal(view.rejection_count,2);assert.equal(view.rejection_history_complete,true);
      assert.equal(view.active_count,4);assert.equal(view.retained_active_count,4);assert.equal(view.active_history_complete,true);
      const read=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
      await page.locator('#reload').click();await (await read).finished();
      await page.locator('[data-queue-rejection="queue-desktop-quota-refused"]').getByText('principal_quota_full',{exact:true}).waitFor();
      await page.locator('[data-queue-rejection="queue-desktop-full-refused"]').getByText('queue_full',{exact:true}).waitFor();
      assert.match(await page.locator('.queue-rejection-count').innerText(),/保存 2 件拒絕紀錄；本頁列出 2 件/);
      assert.match(await page.locator('.queue-active-count').innerText(),/目前占用待處理名額 4 件/);
      assert.equal(await page.locator('[data-queue-run="queue-desktop-quota-refused"]').count(),0);
      assert.equal(await page.locator('[data-queue-run="queue-desktop-full-refused"]').count(),0);
      const after=await inspect();assert.equal(after.queue_sha256,before.queue_sha256);assert.equal(after.capacity_sha256,before.capacity_sha256);
      assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);assert.deepEqual(after.budget,before.budget);
      report.refusals={quota_status:quota.status(),full_status:full.status(),count:view.rejection_count,active:view.active_count};
      await shot('12-original-quota-and-full-refusals');
      assert.equal((await context.request.post(ready.origin+'/v1/tasks/'+studioExtra.run_id+'/cancel',{headers,data:{}})).status(),200);
      for(const next of batchTasks)assert.equal((await context.request.post(ready.origin+'/v1/tasks/'+next.run_id+'/cancel',{headers:batchHeaders,data:{}})).status(),200);
    });
    await step('saved_original_mode_pool_tariff_and_capacity_binding_survive_current_control_changes',async()=>{
      const frozen=original.queue.frozen;
      assert.equal(frozen.monitor.mode,'cloudyfan');assert.equal(frozen.monitor.tariff.multiplier,'1');
      assert.equal(frozen.access_expires_at,original.queue.accepted_at+30*86400);
      const before=await inspect(),operatorHeaders={Authorization:'Bearer '+ready.operator_token};
      const changedMode=await context.request.post(ready.origin+'/monitor/mode',{headers:operatorHeaders,
        data:{revision:before.control.revision,mode:'fixed_high'}});
      assert.equal(changedMode.status(),200);assert.equal((await changedMode.json()).mode,'fixed_high');
      const changedTariff=await context.request.post(ready.origin+'/monitor/billing/settings',{headers:operatorHeaders,
        data:{...before.billing.settings,multiplier:'2'}});
      assert.equal(changedTariff.status(),200);const newTariff=await changedTariff.json();assert.equal(newTariff.multiplier,'2');
      try {
        const own=await context.request.get(ready.origin+'/v1/tasks/'+task.run_id,{headers});
        assert.equal(own.status(),202);assert.deepEqual((await own.json()).queue.frozen,frozen);
        const beforeRead=await inspect();
        const read=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
        await page.locator('#reload').click();await (await read).finished();
        const detail=page.locator('[data-queue-run="queue-desktop-one"] details');
        await detail.locator('summary').focus();await page.keyboard.press('Enter');
        assert.equal(await detail.locator('[data-queue-frozen="mode"]').innerText(),'cloudyfan');
        assert.equal(await detail.locator('[data-queue-frozen="pool"]').innerText(),frozen.monitor.pool_version);
        assert.equal(await detail.locator('[data-queue-frozen="category"]').innerText(),ready.category);
        assert.equal(await detail.locator('[data-queue-frozen="contract"]').innerText(),frozen.contract_id);
        assert.equal(await detail.locator('[data-queue-frozen="tariff-multiplier"]').innerText(),'1');
        assert.equal(await detail.locator('[data-queue-frozen="tariff-sha"]').innerText(),frozen.monitor.tariff_sha256);
        assert.equal(await detail.locator('[data-queue-frozen="capacity-policy"]').innerText(),frozen.capacity.policy_version);
        assert.equal(await detail.locator('[data-queue-frozen="capacity-configuration"]').innerText(),frozen.capacity.configuration_version);
        assert.equal(await detail.locator('[data-queue-frozen="configuration-sha"]').innerText(),frozen.capacity.configuration_sha256);
        assert.equal(await detail.locator('[data-queue-frozen="registered-sha"]').innerText(),frozen.registered_sha256);
        const afterRead=await inspect();assert.deepEqual(afterRead.model_requests,beforeRead.model_requests);
        assert.equal(afterRead.queue_sha256,beforeRead.queue_sha256);assert.equal(afterRead.capacity_sha256,beforeRead.capacity_sha256);
        assert.deepEqual(afterRead.original_evidence_sha256,beforeRead.original_evidence_sha256);assert.deepEqual(afterRead.budget,beforeRead.budget);
        report.frozen_binding={saved:frozen,current_mode:'fixed_high',current_tariff_multiplier:newTariff.multiplier};
        await shot('13-saved-original-binding');
      } finally {
        const current=await inspect();
        assert.equal((await context.request.post(ready.origin+'/monitor/mode',{headers:operatorHeaders,
          data:{revision:current.control.revision,mode:before.control.mode}})).status(),200);
        assert.equal((await context.request.post(ready.origin+'/monitor/billing/settings',{headers:operatorHeaders,
          data:{...before.billing.settings,revision:newTariff.revision}})).status(),200);
      }
    });
    await step('real_poll_preserves_keyboard_details_and_waiting_cancel_is_terminal_without_POST',async()=>{
      const job=page.locator('[data-queue-run="queue-desktop-one"]');
      const detail=job.locator('details');await detail.locator('summary').focus();
      if(await detail.getAttribute('open')===null)await page.keyboard.press('Enter');
      assert.equal(await detail.getAttribute('open'),'');
      const before=await inspect();
      const polled=page.waitForResponse(response=>response.url().endsWith('/monitor/queue')&&response.request().method()==='GET');
      await (await polled).finished();
      await page.waitForFunction(()=>document.activeElement?.closest('[data-queue-detail]')?.open===true);
      const afterPoll=await inspect();assert.deepEqual(afterPoll.model_requests,before.model_requests);
      assert.deepEqual(afterPoll.budget,before.budget);assert.deepEqual(afterPoll.billing,before.billing);
      assert.equal(afterPoll.queue_sha256,before.queue_sha256);assert.equal(afterPoll.capacity_sha256,before.capacity_sha256);
      assert.deepEqual(afterPoll.original_evidence_sha256,before.original_evidence_sha256);
      await inspect({command:'advance',seconds:3});
      const cancelled=await context.request.post(ready.origin+'/v1/tasks/'+task.run_id+'/cancel',{headers,data:{}});
      assert.equal(cancelled.status(),200);const cancelledView=await cancelled.json();
      assert.equal(cancelledView.status,'cancelled');assert.equal(cancelledView.reason,'cancelled_before_dispatch');
      assert.equal(cancelledView.provider_cancellation_confirmed,false);
      assert.equal(cancelledView.queue.deadline,original.queue.deadline);assert.equal(cancelledView.end_to_end_seconds,3);
      const frozen=await inspect();
      const replay=await context.request.post(ready.origin+'/v1/tasks',{headers,data:task});
      assert.equal(replay.status(),200);assert.deepEqual(await replay.json(),cancelledView);
      const own=await context.request.get(ready.origin+'/v1/tasks/'+task.run_id,{headers});
      assert.equal(own.status(),200);assert.deepEqual(await own.json(),cancelledView);
      const refreshed=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
      await page.locator('#reload').click();await (await refreshed).finished();
      await job.getByText('派送前已取消',{exact:true}).waitFor();
      assert.equal(await detail.getAttribute('open'),'');
      assert.equal(await job.locator('[data-queue-timing="end_to_end"]').innerText(),'3.00 秒');
      const after=await inspect();assert.equal(after.model_requests.length,0);
      assert.deepEqual(after.budget,frozen.budget);assert.equal(after.queue_sha256,frozen.queue_sha256);
      assert.equal(after.capacity_sha256,frozen.capacity_sha256);assert.deepEqual(after.original_evidence_sha256,frozen.original_evidence_sha256);
      assert.ok(!(await page.locator('#queue-page').innerText()).includes('PRIVATE_'));
      await shot('02-cancelled-readonly-keyboard');
    });
    await step('held_actual_POST_keeps_reads_responsive_and_running_cancel_is_request_only',async()=>{
      const runningTask={...task,run_id:'queue-desktop-running'};
      const acceptedRunning=await context.request.post(ready.origin+'/v1/tasks',{headers,data:runningTask});
      assert.equal(acceptedRunning.status(),202);const acceptedView=await acceptedRunning.json();
      await inspect({command:'hold'});await inspect({command:'advance',seconds:2});await inspect({command:'dispatch'});
      let held;
      for(let attempt=0;attempt<60;attempt++) {held=await inspect();if(held.model_response_held)break;await page.waitForTimeout(25);}
      assert.equal(held.model_response_held,true);assert.equal(held.model_requests.length,1);
      const waitingTask={...task,run_id:'queue-desktop-waiting-slot'};
      const waitingHeaders={Authorization:'Bearer '+ready.credentials.agent};
      assert.equal((await context.request.post(ready.origin+'/v1/tasks',{headers:waitingHeaders,data:waitingTask})).status(),202);
      await inspect({command:'dispatch'});
      let waiting;
      for(let attempt=0;attempt<40;attempt++) {
        const response=await context.request.get(ready.origin+'/v1/tasks/'+waitingTask.run_id,{headers:waitingHeaders});
        assert.equal(response.status(),202);waiting=await response.json();if(waiting.reason==='queue_waiting_slot')break;
        await page.waitForTimeout(25);
      }
      assert.equal(waiting.reason,'queue_waiting_slot');assert.equal(waiting.queue_seconds,null);
      const beforeReads=await inspect();
      const refreshed=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
      await page.locator('#reload').click();await (await refreshed).finished();
      const runningJob=page.locator('[data-queue-run="queue-desktop-running"]');
      await runningJob.getByText('執行中',{exact:true}).waitFor();
      assert.equal(await runningJob.locator('[data-queue-timing="queue"]').innerText(),'2.00 秒');
      assert.match(await page.locator('[data-queue-run="queue-desktop-waiting-slot"]').innerText(),/queue_waiting_slot/);
      assert.equal((await inspect()).model_response_held,true);
      const requested=await context.request.post(ready.origin+'/v1/tasks/'+runningTask.run_id+'/cancel',{headers,data:{}});
      assert.equal(requested.status(),200);const requestView=await requested.json();
      assert.equal(requestView.status,'running');assert.equal(requestView.cancellation_requested,true);
      assert.equal(requestView.provider_cancellation_confirmed,false);assert.equal(requestView.queue.deadline,acceptedView.queue.deadline);
      assert.equal(requestView.costs.complete_api_usd,null);assert.equal(requestView.costs.unknown_events,1);
      const reloadRequested=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
      await page.locator('#reload').click();await (await reloadRequested).finished();
      await runningJob.getByText('已提出取消要求；未確認供應商停止，不代表退款或可安全重送。',{exact:true}).waitFor();
      const runningDetail=runningJob.locator('details');await runningDetail.locator('summary').focus();await page.keyboard.press('Enter');
      assert.match(await runningDetail.innerText(),/完整 API 費用\s+未知/);
      assert.match(await runningDetail.innerText(),/費用未明事件\s+1/);
      const afterReads=await inspect();assert.equal(afterReads.model_response_held,true);
      assert.deepEqual(afterReads.model_requests,beforeReads.model_requests);assert.deepEqual(afterReads.budget,beforeReads.budget);
      assert.deepEqual(afterReads.original_evidence_sha256,beforeReads.original_evidence_sha256);
      await shot('03-running-cancel-request-and-wait-slot');
      await inspect({command:'advance',seconds:4});await inspect({command:'release'});
      let completed;
      for(let attempt=0;attempt<60;attempt++) {
        const response=await context.request.get(ready.origin+'/v1/tasks/'+runningTask.run_id,{headers});
        assert.equal(response.status(),200);completed=await response.json();if(completed.queue_state==='completed')break;
        await page.waitForTimeout(25);
      }
      assert.equal(completed.status,'passed');assert.equal(completed.cancellation_requested,true);
      assert.equal(completed.queue_seconds,2);assert.equal(completed.end_to_end_seconds,6);
      assert.equal(completed.provider_cancellation_confirmed,false);
      assert.equal((await inspect()).model_requests.length,1);
      const cancelWaiting=await context.request.post(ready.origin+'/v1/tasks/'+waitingTask.run_id+'/cancel',{headers:waitingHeaders,data:{}});
      assert.equal(cancelWaiting.status(),200);assert.equal((await cancelWaiting.json()).status,'cancelled');
    });
    await step('real_operator_token_rotation_clears_old_queue_data_until_fresh_authentication',async()=>{
      const before=await inspect();
      const unauthorized=page.waitForResponse(response=>response.url().endsWith('/monitor/queue')&&response.status()===401);
      const rotated=await inspect({command:'rotate_operator'});
      await (await unauthorized).finished();await page.locator('#login[open]').waitFor();
      report.expected_http_errors.push({method:'GET',path:'/monitor/queue',status:401,code:'unauthorized'},
        {method:'GET',path:'/monitor/snapshot',status:401,code:'unauthorized'});
      assert.equal(await page.locator('#queue-page [data-queue-run]').count(),0);
      assert.ok(!(await page.locator('#queue-page').innerText()).includes('queue-desktop'));
      await page.waitForFunction(()=>document.activeElement?.id==='token');
      const originalToken=await context.request.get(ready.origin+'/monitor/queue',{headers:{Authorization:'Bearer '+ready.operator_token}});
      assert.equal(originalToken.status(),401);
      await shot('04-rotated-operator-clears-private-view');
      await page.locator('#token').fill(rotated.reauth_token);await page.getByRole('button',{name:'登入',exact:true}).click();
      await page.locator('#login[open]').waitFor({state:'hidden'});
      await page.locator('[data-queue-run="queue-desktop-running"]').getByText('通過',{exact:true}).waitFor();
      ready.operator_token=rotated.reauth_token;
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);
      assert.deepEqual(after.budget,before.budget);assert.equal(after.queue_sha256,before.queue_sha256);
      assert.equal(after.capacity_sha256,before.capacity_sha256);assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
    });
    await step('failed_read_retains_original_time_details_and_evidence_without_dispatch_or_source_refresh',async()=>{
      const detail=page.locator('[data-queue-run="queue-desktop-running"] details');
      await detail.locator('summary').focus();await page.keyboard.press('Enter');
      const observed=await page.locator('#queue-observed-at').innerText();const before=await inspect();
      const failed=page.waitForResponse(response=>response.url().endsWith('/monitor/queue')&&response.status()===503);
      await inspect({command:'fail_read'});await (await failed).finished();
      report.expected_http_errors.push({method:'GET',path:'/monitor/queue',status:503,code:'controlled_read_fault'});
      await page.getByText('本次讀取失敗，保留上次畫面與原始觀測時間。',{exact:true}).waitFor();
      assert.equal(await page.locator('#queue-observed-at').innerText(),observed);
      assert.equal(await detail.getAttribute('open'),'');
      await page.waitForFunction(()=>document.activeElement?.closest('[data-queue-detail]')?.open===true);
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);
      assert.deepEqual(after.budget,before.budget);assert.deepEqual(after.billing,before.billing);
      assert.equal(after.queue_sha256,before.queue_sha256);assert.equal(after.capacity_sha256,before.capacity_sha256);
      assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
      assert.deepEqual(after.source_requests,[]);assert.deepEqual(after.accounting_requests,[]);
      await shot('05-stale-read-keeps-original-time');await inspect({command:'restore_read'});
      const recovered=page.waitForResponse(response=>response.url().endsWith('/monitor/queue')&&response.status()===200);
      await page.locator('#reload').click();await (await recovered).finished();
      assert.equal(await page.getByText('本次讀取失敗，保留上次畫面與原始觀測時間。',{exact:true}).count(),0);
    });
    await step('delayed_original_HTTP_response_cannot_overwrite_new_login_or_another_page',async()=>{
      let gate;
      const intercept=async route=>{
        const current=gate;
        if(!current||current.taken)return route.continue();
        current.taken=true;current.request=route.request();const originalResponse=await route.fetch();current.received=true;
        await current.wait;await route.fulfill({response:originalResponse});
      };
      const hold=()=>{const current={taken:false,received:false};current.wait=new Promise(resolve=>{current.release=resolve;});gate=current;return current;};
      const received=async current=>{
        for(let attempt=0;attempt<120&&!current.received;attempt++)await page.waitForTimeout(25);
        assert.equal(current.received,true,'The delayed read must come from the actual loopback service');
      };
      await context.route('**/monitor/queue',intercept);
      try {
        const old=hold();const oldResponse=page.waitForResponse(response=>response.request()===old.request);
        await page.locator('#reload').click();await received(old);
        const freshTask={...task,run_id:'queue-desktop-new-auth-context'};
        const batchHeaders={Authorization:'Bearer '+ready.credentials.batch};
        assert.equal((await context.request.post(ready.origin+'/v1/tasks',{headers:batchHeaders,data:freshTask})).status(),202);
        const before=await inspect();
        await page.locator('#logout').click();await page.locator('#token').fill(ready.operator_token);
        await page.getByRole('button',{name:'登入',exact:true}).click();await page.locator('#login[open]').waitFor({state:'hidden'});
        await page.locator('[data-queue-run="queue-desktop-new-auth-context"]').waitFor();
        old.release();await (await oldResponse).finished();
        assert.equal(await page.locator('[data-queue-run="queue-desktop-new-auth-context"]').count(),1);
        const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);
        assert.deepEqual(after.budget,before.budget);assert.equal(after.queue_sha256,before.queue_sha256);
        assert.equal(after.capacity_sha256,before.capacity_sha256);assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
        const leaving=hold();const leavingResponse=page.waitForResponse(response=>response.request()===leaving.request);
        await page.locator('#reload').click();await received(leaving);
        await page.locator('a[data-page="tasks"]').click();await page.locator('#task-search').waitFor();
        await page.locator('#task-search').focus();leaving.release();await (await leavingResponse).finished();
        assert.equal(await page.locator('#queue-page').count(),0);
        await page.waitForFunction(()=>document.activeElement?.id==='task-search');
        await page.locator('a[data-page="queue"]').click();
        await page.locator('[data-queue-run="queue-desktop-new-auth-context"]').waitFor();
        await shot('06-new-auth-and-page-context');
        assert.equal((await context.request.post(ready.origin+'/v1/tasks/'+freshTask.run_id+'/cancel',{headers:batchHeaders,data:{}})).status(),200);
      } finally {gate?.release();await context.unroute('**/monitor/queue',intercept);}
    });
    await step('waiting_after_a_paid_response_retains_original_fee_and_cancel_stops_only_the_next_dispatch',async()=>{
      await inspect({command:'fail_first_tier'});
      const continuationTask={...task,run_id:'queue-desktop-cancel-continuation'};
      const acceptedContinuation=await context.request.post(ready.origin+'/v1/tasks',{headers,data:continuationTask});
      assert.equal(acceptedContinuation.status(),202);const originalContinuation=await acceptedContinuation.json();
      await inspect({command:'dispatch'});
      let waiting;
      for(let attempt=0;attempt<80;attempt++) {
        const current=await inspect();
        if(current.model_requests.length===2&&!current.worker_running) {
          const response=await context.request.get(ready.origin+'/v1/tasks/'+continuationTask.run_id,{headers});
          assert.equal(response.status(),202);waiting=await response.json();break;
        }
        await page.waitForTimeout(25);
      }
      assert.equal(waiting?.queue_state,'queued');assert.equal(waiting.reason,'queue_waiting_worker');
      assert.equal(Number(waiting.costs.known_api_usd),.0001);assert.equal(waiting.costs.complete_api_usd,null);
      const refreshed=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
      await page.locator('#reload').click();await (await refreshed).finished();
      const job=page.locator('[data-queue-run="queue-desktop-cancel-continuation"]');await job.waitFor();
      assert.ok(!(await job.innerText()).includes('尚未送出模型請求'));
      await inspect({command:'advance',seconds:4});
      const cancelled=await context.request.post(ready.origin+'/v1/tasks/'+continuationTask.run_id+'/cancel',{headers,data:{}});
      assert.equal(cancelled.status(),200);const stopped=await cancelled.json();
      assert.equal(stopped.reason,'cancelled_before_next_dispatch');assert.equal(stopped.queue.deadline,originalContinuation.queue.deadline);
      assert.equal(stopped.queue.waiting_seconds,4);assert.equal(stopped.end_to_end_seconds,4);
      assert.equal(Number(stopped.costs.known_api_usd),.0001);assert.equal(stopped.costs.unknown_events,0);
      const finalRead=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
      await page.locator('#reload').click();await (await finalRead).finished();
      await job.getByText('已取消後續等待',{exact:true}).waitFor();
      assert.match(await job.innerText(),/cancelled_before_next_dispatch/);
      assert.ok(!(await job.innerText()).includes('派送前已取消'));
      assert.equal(await job.locator('[data-queue-timing="waiting"]').innerText(),'4.00 秒');
      const before=await inspect();assert.equal(before.model_requests.length,2);assert.equal(before.budget.events.length,2);
      assert.equal(Number(before.budget.reserved_usd),0);
      const replay=await context.request.post(ready.origin+'/v1/tasks',{headers,data:continuationTask});
      assert.equal(replay.status(),200);assert.deepEqual(await replay.json(),stopped);
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      assert.equal(after.queue_sha256,before.queue_sha256);assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
      await shot('07-cancel-only-next-dispatch-keeps-fee');await inspect({command:'restore_model'});
    });
    await step('two_clients_keep_own_scope_and_original_credential_revocation_refuses_dispatch',async()=>{
      const agentHeaders={Authorization:'Bearer '+ready.credentials.agent};
      const revokedTask={...task,run_id:'queue-desktop-revoked-credential',prompt:'Return only 1+1. PRIVATE_QUEUE_AGENT_PROMPT_MARKER'};
      assert.equal((await context.request.post(ready.origin+'/v1/tasks',{headers:agentHeaders,data:revokedTask})).status(),202);
      const own=await context.request.get(ready.origin+'/v1/tasks/'+revokedTask.run_id,{headers:agentHeaders});
      assert.equal(own.status(),202);const ownView=JSON.stringify(await own.json());
      for(const privateValue of ['PRIVATE_','synthetic-binding','synthetic-shared-account','physical_groups',ready.credentials.studio])assert.ok(!ownView.includes(privateValue));
      assert.equal((await context.request.get(ready.origin+'/v1/tasks/'+revokedTask.run_id,{headers})).status(),404);
      const cross=await context.request.get(ready.origin+'/v1/tasks/queue-desktop-running',{headers:agentHeaders});assert.equal(cross.status(),404);
      for(const clientHeaders of [headers,agentHeaders]) {
        const forbidden=await context.request.get(ready.origin+'/monitor/queue',{headers:clientHeaders});assert.equal(forbidden.status(),401);
      }
      const ownMonitor=await context.request.get(ready.origin+'/v1/monitor',{headers:agentHeaders});assert.equal(ownMonitor.status(),200);
      const monitorView=JSON.stringify(await ownMonitor.json());assert.ok(!monitorView.includes('queue-desktop-running'));assert.ok(!monitorView.includes('PRIVATE_'));
      const before=await inspect();await inspect({command:'revoke_agent'});
      assert.equal((await context.request.get(ready.origin+'/v1/tasks/'+revokedTask.run_id,{headers:agentHeaders})).status(),401);
      await inspect({command:'dispatch'});
      for(let attempt=0;attempt<80&&(await inspect()).worker_running;attempt++)await page.waitForTimeout(25);
      const refreshed=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
      await page.locator('#reload').click();await (await refreshed).finished();
      const job=page.locator('[data-queue-run="queue-desktop-revoked-credential"]');await job.getByText('已拒絕',{exact:true}).waitFor();
      assert.match(await job.innerText(),/queue_original_authority_denied/);
      const operatorText=await page.locator('#queue-page').innerText();
      for(const privateValue of ['PRIVATE_',ready.operator_token,...Object.values(ready.credentials)])assert.ok(!operatorText.includes(privateValue));
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);
      assert.deepEqual(after.budget,before.budget);assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
      assert.deepEqual(after.source_requests,[]);assert.deepEqual(after.accounting_requests,[]);
      await shot('08-revoked-original-credential-safe-refusal');
    });
    await step('original_wait_deadline_expires_without_POST_or_polling_mutation_and_replay_stays_expired',async()=>{
      const batchHeaders={Authorization:'Bearer '+ready.credentials.batch};
      const expiredTask={...task,run_id:'queue-desktop-original-deadline'};
      const acceptedExpiry=await context.request.post(ready.origin+'/v1/tasks',{headers:batchHeaders,data:expiredTask});
      assert.equal(acceptedExpiry.status(),202);const originalExpiry=await acceptedExpiry.json();
      assert.equal(originalExpiry.queue.deadline-originalExpiry.queue.accepted_at,120);
      await inspect({command:'advance',seconds:121});const before=await inspect();
      const own=await context.request.get(ready.origin+'/v1/tasks/'+expiredTask.run_id,{headers:batchHeaders});
      assert.equal(own.status(),410);const expired=await own.json();
      assert.equal(expired.status,'expired');assert.equal(expired.reason,'queue_deadline_expired');
      assert.equal(expired.queue.deadline,originalExpiry.queue.deadline);assert.equal(expired.queue.waiting_seconds,120);
      assert.equal(expired.queue_seconds,null);assert.equal(expired.end_to_end_seconds,120);assert.equal(expired.queue.effective_expiry_only,true);
      const replay=await context.request.post(ready.origin+'/v1/tasks',{headers:batchHeaders,data:expiredTask});
      assert.equal(replay.status(),410);assert.deepEqual(await replay.json(),expired);
      const refreshed=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
      await page.locator('#reload').click();await (await refreshed).finished();
      const job=page.locator('[data-queue-run="queue-desktop-original-deadline"]');await job.getByText('已到期',{exact:true}).waitFor();
      assert.match(await job.innerText(),/queue_deadline_expired/);
      assert.equal(await job.locator('[data-queue-timing="waiting"]').innerText(),'120.00 秒');
      assert.equal(await job.locator('[data-queue-timing="end_to_end"]').innerText(),'120.00 秒');
      await job.locator('summary').focus();await page.keyboard.press('Enter');
      await job.getByText('目前依原期限顯示到期；此唯讀觀測不代表派送程序已寫入新的狀態。',{exact:true}).waitFor();
      const cancelled=await context.request.get(ready.origin+'/v1/tasks/queue-desktop-one',{headers});
      assert.equal(cancelled.status(),200);const cancelledView=await cancelled.json();
      assert.equal(cancelledView.queue.waiting_seconds,3);assert.equal(cancelledView.end_to_end_seconds,3);
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);
      assert.deepEqual(after.budget,before.budget);assert.equal(after.queue_sha256,before.queue_sha256);
      assert.equal(after.capacity_sha256,before.capacity_sha256);assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
      await shot('09-original-expiry-is-readonly');
    });
    await step('bounded_history_totals_are_honest_and_a_naturally_removed_focused_row_falls_back_to_reload',async()=>{
      const operatorHeaders={Authorization:'Bearer '+ready.operator_token};
      const batchHeaders={Authorization:'Bearer '+ready.credentials.batch};
      const initial=await context.request.get(ready.origin+'/monitor/queue',{headers:operatorHeaders});
      assert.equal(initial.status(),200);const initialCount=(await initial.json()).job_count;
      const perishable={...task,run_id:'queue-desktop-history-focus'};
      assert.equal((await context.request.post(ready.origin+'/v1/tasks',{headers:batchHeaders,data:perishable})).status(),202);
      assert.equal((await context.request.post(ready.origin+'/v1/tasks/'+perishable.run_id+'/cancel',{headers:batchHeaders,data:{}})).status(),200);
      const firstRead=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'));
      await page.locator('#reload').click();await (await firstRead).finished();
      const detail=page.locator('[data-queue-run="queue-desktop-history-focus"] details');
      await detail.locator('summary').focus();await page.keyboard.press('Enter');
      await page.waitForFunction(()=>document.activeElement?.closest('[data-queue-run]')?.dataset.queueRun==='queue-desktop-history-focus');
      const before=await inspect();
      for(let number=0;number<100;number++) {
        const next={...task,run_id:'queue-desktop-history-'+String(number).padStart(3,'0')};
        assert.equal((await context.request.post(ready.origin+'/v1/tasks',{headers:batchHeaders,data:next})).status(),202);
        assert.equal((await context.request.post(ready.origin+'/v1/tasks/'+next.run_id+'/cancel',{headers:batchHeaders,data:{}})).status(),200);
      }
      const snapshot=await context.request.get(ready.origin+'/monitor/queue',{headers:operatorHeaders,timeout:20000});
      assert.equal(snapshot.status(),200);const view=await snapshot.json();
      assert.equal(view.job_count,initialCount+101);assert.equal(view.jobs.length,100);assert.equal(view.history_complete,false);
      assert.ok(!view.jobs.some(item=>item.run_id===perishable.run_id));
      const lastRead=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'),{timeout:20000});
      await (await lastRead).finished();
      await page.waitForFunction(()=>document.querySelectorAll('#queue-page [data-queue-run]').length===100,null,{timeout:20000});
      await page.waitForFunction(()=>!document.querySelector('[data-queue-run="queue-desktop-history-focus"]'),null,{timeout:20000});
      assert.match(await page.locator('.queue-count').innerText(),new RegExp('保存 '+(initialCount+101)+' 件工作；本頁列出 100 件'));
      assert.match(await page.locator('.queue-count').innerText(),/僅為部分歷史/);
      await page.waitForFunction(()=>document.activeElement?.id==='reload',null,{timeout:20000});
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      report.bounded_history={total:view.job_count,visible:view.jobs.length,history_complete:view.history_complete,
        real_unpaid_cancelled_jobs:101,fixture_read_timeout_ms:20000,retained_run:'queue-desktop-history-099',
        focus_fallback_trigger:'ordinary periodic poll after genuine history truncation'};
      await shot('11-bounded-history-focus-fallback',{fullPage:false});
    });
    await step('thirty_day_access_is_not_extended_by_waiting_replay_or_resume_and_retained_view_stays_safe',async()=>{
      await inspect({command:'advance',seconds:30*86400+1});const before=await inspect();
      const originalRunning={...task,run_id:'queue-desktop-running'};
      const status=await context.request.get(ready.origin+'/v1/tasks/'+originalRunning.run_id,{headers});
      assert.equal(status.status(),410);assert.equal((await status.json()).error.code,'task_expired');
      const result=await context.request.get(ready.origin+'/v1/tasks/'+originalRunning.run_id+'/result',{headers});
      assert.equal(result.status(),410);assert.equal((await result.json()).error.code,'task_expired');
      const replay=await context.request.post(ready.origin+'/v1/tasks',{headers,data:originalRunning});
      assert.equal(replay.status(),410);assert.equal((await replay.json()).error.code,'task_expired');
      const resume=await context.request.post(ready.origin+'/v1/tasks/'+originalRunning.run_id+'/resume',{headers,data:{}});
      assert.equal(resume.status(),410);assert.equal((await resume.json()).error.code,'task_expired');
      const projection=await context.request.get(ready.origin+'/monitor/queue',{
        headers:{Authorization:'Bearer '+ready.operator_token},timeout:20000});
      assert.equal(projection.status(),200);
      const retained=(await projection.json()).jobs.find(item=>item.run_id===report.bounded_history.retained_run);
      assert.ok(retained);assert.equal(retained.observation_error,'task_expired');
      const refreshed=page.waitForResponse(response=>response.url().endsWith('/monitor/queue'),{timeout:20000});
      await page.locator('#reload').click();await (await refreshed).finished();
      const expired=page.locator('[data-queue-run="'+(report.bounded_history?.retained_run||'queue-desktop-original-deadline')+'"]');
      await expired.getByText('存取已到期',{exact:true}).waitFor();
      assert.match(await expired.innerText(),/task_expired/);assert.ok(!(await expired.innerText()).includes('排隊中'));
      assert.ok(!(await page.locator('#queue-page').innerText()).includes('PRIVATE_'));
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      assert.equal(after.queue_sha256,before.queue_sha256);assert.equal(after.capacity_sha256,before.capacity_sha256);
      assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);
      report.retention={access_seconds:30*86400,status:status.status(),result:result.status(),replay:replay.status(),resume:resume.status(),
        retained_historical_reason:retained.reason,observation_error:retained.observation_error};
      await shot('10-retained-metadata-access-expired',{fullPage:false});
    });
    }
    const final=await inspect();
    assert.deepEqual(final.source_requests,[]);assert.deepEqual(final.accounting_requests,[]);
    assert.deepEqual(final.blocked_server_socket_attempts,[]);assert.equal(final.worker_running,false);
    assert.deepEqual(final.worker_results.filter(item=>item.status==='failed'),[]);
    report.final_evidence={model_POSTs:final.model_requests.length,source_requests:final.source_requests.length,
      accounting_requests:final.accounting_requests.length,blocked_server_sockets:final.blocked_server_socket_attempts.length,
      original_intents:Object.keys(final.original_evidence_sha256).filter(name=>name.endsWith('/intent.json')).length,
      original_responses:Object.keys(final.original_evidence_sha256).filter(name=>name.endsWith('/response.json')).length};
    report.console_classification=report.console_errors.map(message=>({message,
      classification:/status of (401|503)\b/.test(message)&&report.expected_http_errors.some(item=>message.includes(String(item.status)))?
        'expected_server_auth_rotation_or_controlled_read_fault':'unexpected'}));
    assert.ok(report.console_classification.every(item=>item.classification!=='unexpected'));
    assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.blocked_browser_requests,[]);report.status='passed';
  } catch(error) {
    report.status='failed';report.failure={name:error.name,message:error.message,stack:error.stack};process.exitCode=1;
    if(page)await shot('failure').catch(()=>{});
  } finally {
    if(browser)await browser.close();report.fixture_stop=await fixture.stop();
    if(report.fixture_stop.code!==0||report.fixture_stop.timed_out||report.fixture_stop.cleanup_failed) {
      report.status='failed';process.exitCode=1;report.cleanup_failure='Private fixture did not exit cleanly';
    }
    if(report.source_sha256&&report.core_source_sha256) {
      const after=Object.fromEntries(await Promise.all(names.map(async name=>[name,await hash(path.join(root,name))])));
      const coreAfter=Object.fromEntries(await Promise.all(coreNames.map(async name=>[name,await hash(path.join(core,'src/cloudyfan_jev',name))])));
      report.source_inputs_unchanged=JSON.stringify(after)===JSON.stringify(report.source_sha256)&&JSON.stringify(coreAfter)===JSON.stringify(report.core_source_sha256);
      if(!report.source_inputs_unchanged){report.status='failed';process.exitCode=1;report.input_failure='Source inputs changed during desktop QA';}
    }
    await fs.writeFile(path.join(output,'desktop-receipt.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({status:report.status,checks:report.checks.length,output}));
  }
}
main().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
