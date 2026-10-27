#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {execFileSync} = require('node:child_process');
const {startFixture} = require('./qa_fixture_ipc.cjs');

async function safeAria(page) {
  const passwords=await page.locator('input[type="password"]').evaluateAll(nodes=>nodes.map(node=>node.value).filter(Boolean));
  let text=await page.locator('body').ariaSnapshot();
  for(const value of passwords)text=text.split(value).join('[REDACTED_CREDENTIAL]');
  return text;
}

async function storageMain() {
  const root=path.resolve(__dirname,'..'),core=process.env.CLOUDYFAN_CORE_ROOT;
  assert.ok(core,'An explicitly matched Core root is required');
  assert.ok(process.argv[2],'Use a new private output directory');
  const output=path.resolve(process.argv[2]);
  await fs.mkdir(path.dirname(output),{recursive:true});
  await fs.mkdir(output,{recursive:false,mode:0o700});
  const {chromium}=require(process.env.PLAYWRIGHT_PATH||'/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
  const owned=['src/cloudyfan_monitor/static/capacity.js','src/cloudyfan_monitor/static/capacity.css',
    'src/cloudyfan_monitor/static/queue.js','src/cloudyfan_monitor/static/queue.css',
    'scripts/qa_capacity_resilience_http.py','scripts/qa_capacity_resilience_desktop.cjs',
    'tests/test_operator_session_http.py'];
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
    [path.join(__dirname,'qa_capacity_resilience_http.py'),'--output',output,'--minimal'],{cwd:root,
      env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[path.join(root,'src'),path.join(core,'src')].join(path.delimiter)}});
  let browser,page;
  const inspect=async(command={command:'inspect'})=>{fixture.send(command);return fixture.next();};
  const save=async name=>{
    await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});report.screenshots.push(name+'.png');
    await fs.writeFile(path.join(output,name+'.aria.txt'),await safeAria(page));
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
    if(browser){
      const started=performance.now();report.browser_close={attempted:true,resolved:false};
      try{await browser.close();report.browser_close.resolved=true;}
      catch(error){report.browser_close.error_type=error.name;report.status='failed';process.exitCode=1;}
      finally{report.browser_close.duration_ms=performance.now()-started;}
    }
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
async function lifecycleMain() {
  const root=path.resolve(__dirname,'..'),core=process.env.CLOUDYFAN_CORE_ROOT;
  assert.ok(core,'An explicitly matched Core root is required');
  assert.ok(process.argv[2],'Use a new private output directory');
  const restartConsoleObserve=process.argv.includes('--restart-console-observe');
  const caseArguments=process.argv.filter(value=>value.startsWith('--restart-console-case='));
  const restartConsoleCase=caseArguments.length?caseArguments[0].slice('--restart-console-case='.length):'positive';
  assert.ok(caseArguments.length<=1&&['positive','post','outside','forged','candidate-query','query-invalid'].includes(restartConsoleCase),
    'Unknown private stopped-service diagnostic case');
  assert.ok(!caseArguments.length||restartConsoleObserve,'Private cases require the observation diagnostic');
  assert.ok(!restartConsoleObserve||process.argv.includes('--focused-restart'),
    'The private stopped-service observation requires the focused restart slice');
  assert.ok(!restartConsoleObserve||!['--storage','--queue-fault','--local-fault','--companion-loss',
    '--policy-rejection-delay','--operator-session-delay'].some(flag=>process.argv.includes(flag)),
  'Private console diagnostics cannot replace another focused slice');
  const output=path.resolve(process.argv[2]),service=path.join(output,'service');
  await fs.mkdir(path.dirname(output),{recursive:true});
  await fs.mkdir(output,{recursive:false,mode:0o700});
  const {chromium}=require(process.env.PLAYWRIGHT_PATH||'/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
  const owned=['src/cloudyfan_monitor/static/capacity.js','src/cloudyfan_monitor/static/capacity.css',
    'src/cloudyfan_monitor/static/queue.js','src/cloudyfan_monitor/static/queue.css',
    'scripts/qa_capacity_resilience_http.py','scripts/qa_capacity_resilience_desktop.cjs',
    'scripts/qa_capacity_resilience_console.cjs',
    'tests/test_operator_session_http.py'];
  async function capture(directory,extra=[]) {
    const git=(...args)=>execFileSync('git',args,{cwd:directory,encoding:'utf8'}).trim();
    const hashes={};
    for(const name of [...new Set([...git('ls-files','-z').split('\0').filter(Boolean),...extra])].sort()) {
      const target=path.join(directory,name),state=await fs.lstat(target);
      hashes[name]=createHash('sha256').update(state.isSymbolicLink()?Buffer.from(await fs.readlink(target)):await fs.readFile(target)).digest('hex');
    }
    return {head:git('rev-parse','HEAD'),tree:git('rev-parse','HEAD^{tree}'),
      status:git('status','--porcelain=v1','--untracked-files=normal'),sha256:hashes};
  }
  const report={schema_version:1,status:'incomplete',scope:'actual same-service Backup/Health/Queue/Capacity/Windows and original fees; MockTransport only; no real supplier qualification or SLO',
    checks:[],screenshots:[],page_errors:[],console_errors:[],expected_http_errors:[],http_responses:[],
    blocked_browser_requests:[],boots:[],viewport:{width:1280,height:900},
    visual_regression:{status:'INCONCLUSIVE',reason:'No declared visual baseline; actual images and semantic checks retained'},
    accessibility_scope:'Keyboard focus/disclosure at the declared desktop viewport; no screen-reader or WCAG certification'};
  report.planned_check_count=['--queue-fault','--local-fault','--companion-loss','--policy-rejection-delay','--operator-session-delay'].some(flag=>process.argv.includes(flag))?3:process.argv.includes('--focused-restart')?12:13;
  if(process.argv.includes('--policy-rejection-delay'))report.scope+='; tiny genuine policy rejection with bounded delivery of completed HTTP snapshots; N101 not run';
  if(process.argv.includes('--operator-session-delay'))report.scope+='; tiny real operator rotation and completed old-auth delivery barrier; N101 not run';
  if(process.argv.includes('--focused-restart'))report.scope+='; focused restart slice: >100 paid history and refusal steps explicitly not run';
  if(restartConsoleObserve)report.scope+='; private stopped-service browser observation diagnostic; strict outage verdict and original HTTP-status rule';
  const fullWorkflow=report.planned_check_count===13,heldTimeoutSeconds=fullWorkflow?3600:600;
  const heldTimeoutMs=heldTimeoutSeconds*1000;
  report.private_fixture_limits={held_timeout_seconds:heldTimeoutSeconds,idle_maximum_elapsed_ms:120000,
    idle_maximum_samples:1200,idle_sampling_interval_ms:100,
    scope:'Private MockTransport completion gate and observation sampling; no product deadline or SLO change'};
  let liveHoldStarted=null;
  const requireLiveHold=()=>{
    if(liveHoldStarted===null)return;
    const elapsed=performance.now()-liveHoldStarted;
    report.live_hold_guard.maximum_observed_elapsed_ms=Math.max(report.live_hold_guard.maximum_observed_elapsed_ms,elapsed);
    if(elapsed>=heldTimeoutMs){
      report.live_hold_guard.bound_reached=true;
      throw Error('Configured private live-held fixture prerequisite ended; remaining workflow is not accepted');
    }
  };
  const mib=1024*1024;
  report.resource_guard={enforced:fullWorkflow,start_minimum_available_bytes:512*mib,
    minimum_available_bytes:128*mib,maximum_run_output_bytes:256*mib,final_receipt_reserve_bytes:4*mib,
    boundaries:[],scope:'Observed filesystem availability and this new output directory; no deletion or held-response release'};
  const resourceCheck=async(boundary,{start=false}={})=>{
    const disk=await fs.statfs(output),folders=[output];let bytes=0;
    while(folders.length){
      const folder=folders.pop();
      for(const entry of await fs.readdir(folder,{withFileTypes:true})){
        const target=path.join(folder,entry.name);
        if(entry.isDirectory())folders.push(target);
        else bytes+=(await fs.lstat(target)).size;
      }
    }
    const guard=report.resource_guard,row={boundary,at:new Date().toISOString(),
      available_bytes:disk.bavail*disk.bsize,output_bytes:bytes};
    guard.boundaries.push(row);
    guard.minimum_observed_available_bytes=Math.min(guard.minimum_observed_available_bytes??Infinity,row.available_bytes);
    guard.maximum_observed_output_bytes=Math.max(guard.maximum_observed_output_bytes??0,bytes);
    if(start)guard.start_available_bytes=row.available_bytes;
    if(boundary==='after_fixture_exit')guard.end_available_bytes=row.available_bytes;
    await fs.appendFile(path.join(output,'actual-resource-boundaries.jsonl'),JSON.stringify(row)+'\n');
    if(fullWorkflow){
      assert.ok(!start||row.available_bytes>=guard.start_minimum_available_bytes,'Resource guard: insufficient starting filesystem availability');
      assert.ok(row.available_bytes>=guard.minimum_available_bytes,'Resource guard: minimum filesystem availability reached');
      assert.ok(bytes+guard.final_receipt_reserve_bytes<=guard.maximum_run_output_bytes,'Resource guard: bounded new-run output reached');
    }
  };
  let fixture,ready,browser,context,page,boot=0,operator,client,agent,originalWindow,deliveryInterceptor=null;
  let browserObservation,currentStep='setup';
  let consoleFrozen=false;
  const recordConsole=message=>{
    if(message.type()!=='error')return;
    if(consoleFrozen){report.console_errors_after_gate ||= [];report.console_errors_after_gate.push(message.text());}
    else report.console_errors.push(message.text());
  };
  const headers=token=>({Authorization:'Bearer '+token});
  const start=async port=>{
    const bootOutput=path.join(output,'boot-'+String(++boot).padStart(2,'0'));
    fixture=startFixture(path.join(core,'.venv/bin/python'),
      [path.join(__dirname,'qa_capacity_resilience_http.py'),'--output',bootOutput,'--service-root',service,
        ...(fullWorkflow?['--full-workflow']:[]),...(port?['--port',String(port)]:[])],
      {cwd:root,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[path.join(root,'src'),path.join(core,'src')].join(path.delimiter)}});
    ready=await fixture.next();assert.equal(ready.type,'ready');
    assert.equal(ready.held_timeout_seconds,heldTimeoutSeconds,'Actual configured private held-response bound');
    browserObservation?.ready({pid:ready.pid,sameOrigin:ready.origin===report.boots[0].origin,
      samePort:new URL(ready.origin).port===new URL(report.boots[0].origin).port});
    report.boots.push({pid:ready.pid,origin:ready.origin,output:path.basename(bootOutput),
      held_timeout_seconds:ready.held_timeout_seconds});
    operator=headers(ready.operator_token);client=headers(ready.credentials.studio);agent=headers(ready.credentials.agent);
  };
  const inspect=async(command={command:'inspect'})=>{
    const started=performance.now(),row={command:command.command,started_at:new Date().toISOString()};
    try {
      requireLiveHold();
      fixture.send(command);const value=await fixture.next();
      row.response_type=value.type;row.model_request_count=value.model_requests?.length??value.requests_count;
      row.receipt_build_seconds=value.receipt_build_seconds;requireLiveHold();return value;
    } catch(error) {row.failure_type=error.name;row.failure_message=error.message;throw error;}
    finally {row.duration_ms=performance.now()-started;await fs.appendFile(path.join(output,'actual-fixture-durations.jsonl'),JSON.stringify(row)+'\n');}
  };
  const task=run=>({run_id:run,category:ready.category,prompt:'Return only 1+1. PRIVATE_RESILIENCE_PROMPT_CANARY'});
  const api=async(method,url,auth=operator,data)=>{
    const started=performance.now(),row={method:method.toUpperCase(),path:url,started_at:new Date().toISOString()};
    try {
      requireLiveHold();
      const response=await context.request[method](ready.origin+url,{headers:auth,...(data===undefined?{}:{data})});
      const body=await response.json();row.status=response.status();
      if(response.status()>=400)report.expected_http_errors.push({method:row.method,path:url,status:response.status(),code:body.error?.code});
      requireLiveHold();return {response,body};
    } catch(error) {row.failure_type=error.name;throw error;}
    finally {row.duration_ms=performance.now()-started;await fs.appendFile(path.join(output,'actual-client-http-durations.jsonl'),JSON.stringify(row)+'\n');}
  };
  const get=async(url,auth=operator)=>{const row=await api('get',url,auth);assert.equal(row.response.status(),200);return row.body;};
  const shot=async name=>{
    await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});report.screenshots.push(name+'.png');
    await fs.writeFile(path.join(output,name+'.aria.txt'),await safeAria(page));
  };
  const step=async(name,action)=>{
    currentStep=name;
    const row={name,started_at:new Date().toISOString(),passed:false};report.checks.push(row);
    await resourceCheck('before:'+name);
    requireLiveHold();await action();requireLiveHold();
    await resourceCheck('after:'+name);row.passed=true;row.finished_at=new Date().toISOString();
    await fs.writeFile(path.join(output,'desktop-progress.json'),JSON.stringify({checks:report.checks},null,2));
    currentStep='between_steps';
  };
  function unchanged(before,after) {
    for(const key of ['model_requests','source_requests','accounting_requests','budget','health_sqlite_sha256',
      'capacity_sha256','queue_sha256','original_evidence_sha256','wire_files_sha256','raw_mock_request_sha256',
      'raw_mock_method_counts'])assert.deepEqual(after[key],before[key],key);
    assert.equal(after.raw_mock_metadata_status,'complete');
    assert.ok(Object.keys(after.health_sqlite_sha256).length>0,'Real Health SQLite hash set must be nonempty');
    assert.deepEqual(after.blocked_server_socket_attempts,[]);
  }
  let threadDiagnosticRequested=false;
  const idle=async(expected=0)=>{
    const started=performance.now(),row={started_at:new Date().toISOString(),expected_workers:expected,
      maximum_elapsed_ms:120000,maximum_samples:1200,sampling_interval_ms:100,samples:0,completed:false};
    let unchanged;
    try{
      for(let i=0;i<row.maximum_samples&&performance.now()-started<row.maximum_elapsed_ms;i++){
        row.samples++;
        const progress=await inspect({command:'progress'});assert.equal(progress.type,'progress');
        assert.ok(Number.isInteger(progress.requests_count)&&Number.isInteger(progress.thread_running_count));
        if(performance.now()-started>=row.maximum_elapsed_ms)break;
        const observed=performance.now(),signature=[progress.requests_count,progress.thread_running_count].join(':');
        if(!unchanged||unchanged.signature!==signature)unchanged={signature,since:observed};
        if(!threadDiagnosticRequested&&progress.thread_running_count>expected&&observed-unchanged.since>=30000){
          threadDiagnosticRequested=true;
          report.private_thread_diagnostic={requested_at:new Date().toISOString(),boot,
            unchanged_elapsed_ms:observed-unchanged.since,requests_count:progress.requests_count,
            extra_worker_count:progress.thread_running_count-expected};
          const diagnostic=await inspect({command:'thread_dump'});
          assert.equal(diagnostic.type,'thread_dump');assert.equal(diagnostic.ref,'private-all-thread-stacks.txt');
          row.diagnostic_ref=diagnostic.ref;report.private_thread_diagnostic.ref=diagnostic.ref;
        }
        if(progress.thread_running_count===expected){
          const value=await inspect();
          if(value.worker_running_count===expected&&performance.now()-started<=row.maximum_elapsed_ms){row.completed=true;return value;}
        }
        await page.waitForTimeout(row.sampling_interval_ms);
      }
      throw Error('Actual controlled worker did not reach expected completion boundary');
    }finally{
      row.duration_ms=performance.now()-started;
      row.elapsed_bound_reached=row.duration_ms>=row.maximum_elapsed_ms;
      row.sample_bound_reached=row.samples>=row.maximum_samples;
      await fs.appendFile(path.join(output,'actual-private-idle.jsonl'),JSON.stringify(row)+'\n');
    }
  };
  const run=async(runId,auth=client,heldWorkers=0)=>{
    let value;
    for(let attempt=0;attempt<10;attempt++){
      await inspect({command:'work'});await idle(heldWorkers);
      const row=await api('get','/v1/tasks/'+runId,auth);value=row.body;
      if(!['queued','accepting','claiming','running'].includes(value.status))return value;
    }
    throw Error('Task did not reach an actual terminal state in the declared dispatch bound');
  };
  const login=async token=>{
    await page.locator('#token').fill(token);await page.getByRole('button',{name:'登入',exact:true}).click();
    await page.locator('#login[open]').waitFor({state:'hidden'});
  };
  const refresh=async name=>page.evaluate(key=>window[key].refresh(true),name);
  const queuePage=async()=>{await page.locator('a[data-page="queue"]').click();await page.getByRole('heading',{name:'排隊與任務生命週期',exact:true}).waitFor();await refresh('CloudyFanQueue');};
  const capacityPage=async()=>{await page.locator('a[data-page="capacity"]').click();await page.getByRole('heading',{name:'供應商限額來源',exact:true}).waitFor();await refresh('CloudyFanCapacity');};
  try {
    report.source_before={monitor:await capture(root,owned),core:await capture(core)};
    await fs.writeFile(path.join(output,'source-inputs-before.json'),JSON.stringify(report.source_before,null,2));
    await resourceCheck('before_fixture_start',{start:true});
    await start();
    browser=await chromium.launch({channel:'chrome',headless:process.env.CLOUDYFAN_QA_HEADED!=='1'});report.browser=browser.version();
    context=await browser.newContext({viewport:report.viewport,locale:'zh-TW',serviceWorkers:'block'});
    await context.route('**/*',route=>{
      if(new URL(route.request().url()).origin===ready.origin)return deliveryInterceptor?deliveryInterceptor(route):route.continue();
      report.blocked_browser_requests.push(new URL(route.request().url()).origin);return route.abort();
    });
    page=await context.newPage();page.setDefaultTimeout(6000);
    page.on('pageerror',error=>report.page_errors.push(error.message));
    page.on('console',recordConsole);
    page.on('response',response=>report.http_responses.push({method:response.request().method(),path:new URL(response.url()).pathname,status:response.status()}));
    const {observeBrowserConsole}=require('./qa_capacity_resilience_console.cjs');
    browserObservation=await observeBrowserConsole({page,output,origin:ready.origin,category:ready.category,
      step:()=>currentStep,boot:()=>boot});
    browserObservation.ready({pid:ready.pid,initial:true});
    await page.goto(ready.origin);await page.locator('#environment').filter({hasText:/模擬/}).waitFor();
    await page.locator('#logout').click();await login(ready.operator_token);
    await step('actual_configuration_and_explicit_registered_policy_activation',async()=>{
      await capacityPage();
      await page.getByRole('heading',{name:'容量保存狀態可核對',exact:true}).waitFor();
      await page.locator('#capacity-policy-version').selectOption(ready.policy_version);
      await page.locator('#capacity-activate-policy').click();
      await page.locator('#confirm[open]').waitFor();await page.locator('#apply-confirm').click();
      await page.locator('#capacity-dispatch-status').getByText('已明示啟用',{exact:true}).waitFor();
      assert.equal((await get('/monitor/capacity')).dispatch.active_version,ready.policy_version);
      const state=await inspect();assert.equal(state.model_requests.length,0);assert.ok(Object.keys(state.health_sqlite_sha256).length>0);
      await shot('01-explicit-configured-policy');
    });
    await step('actual_paid_availability_fallback_preserves_original_response_and_fee',async()=>{
      await inspect({command:'scenario',name:'availability'});
      const accepted=await api('post','/v1/tasks',client,task('resilience-fallback'));assert.equal(accepted.response.status(),202);
      const completed=await run('resilience-fallback');assert.equal(completed.status,'passed');
      const state=await inspect();assert.equal(state.model_requests.length,2);
      assert.equal(state.budget['budget-primary.sqlite'].known_usd,'0.02');
      assert.deepEqual(state.source_requests,[]);assert.deepEqual(state.accounting_requests,[]);
      const snapshot=await get('/monitor/snapshot');
      await fs.writeFile(path.join(output,'safe-original-fallback.json'),JSON.stringify(snapshot,null,2));
      const cap=await get('/monitor/capacity');
      assert.equal(cap.dispatch.windows.event_count,1);
      originalWindow=cap.dispatch.windows.events[0];
      assert.equal(originalWindow.status,'wait');assert.equal(originalWindow.source_kind,'original_response');
      assert.equal(originalWindow.header,'retry-after');assert.equal(originalWindow.observed_at,state.clock);
      assert.equal(originalWindow.not_before,originalWindow.observed_at+3600);
      await fs.writeFile(path.join(output,'safe-original-window.json'),JSON.stringify(originalWindow,null,2));
      await capacityPage();
      const savedWindow=page.locator('[data-capacity-window="'+originalWindow.id+'"]').first();
      await savedWindow.locator('summary').click();
      assert.match(await savedWindow.innerText(),/已保存原回應/);
      assert.ok(!(await savedWindow.locator('[data-window-time="observed"]').innerText()).includes('未知'));
      assert.ok(!(await savedWindow.locator('[data-window-time="not-before"]').innerText()).includes('未知'));
      await shot('02-original-response-window');
      await queuePage();await page.locator('[data-queue-run="resilience-fallback"]').getByText('通過',{exact:true}).waitFor();
      await shot('02-original-fallback-and-fee');
    });
    if(process.argv.includes('--queue-fault')) {
      await step('actual_queue_schema_loss_retains_original_facts_but_current_count_is_unknown',async()=>{
        const before=await inspect();await inspect({command:'fault_missing_queue_table'});
        const snapshot=await get('/monitor/queue');assert.equal(snapshot.integrity.status,'unavailable');
        assert.equal(snapshot.integrity.automatic_repair,false);
        const after=await inspect();assert.deepEqual(after.budget,before.budget);assert.deepEqual(after.health_sqlite_sha256,before.health_sqlite_sha256);
        assert.deepEqual(after.original_evidence_sha256,before.original_evidence_sha256);assert.deepEqual(after.model_requests,before.model_requests);
        await fs.writeFile(path.join(output,'safe-unavailable-queue.json'),JSON.stringify(snapshot,null,2));
        await refresh('CloudyFanQueue');await page.getByRole('heading',{name:'排隊保存狀態無法核對',exact:true}).waitFor();
        assert.match(await page.locator('#queue-integrity').innerText(),/原保存/);
        assert.match(await page.locator('.queue-active-count').innerText(),/未知/);
        await page.locator('[data-queue-run="resilience-fallback"]').waitFor();
        await shot('03-original-queue-unavailable');
      });
    } else if(process.argv.includes('--local-fault')) {
      await step('actual_local_index_corruption_keeps_original_payload_and_marks_only_affected_group_unknown',async()=>{
        const before=await inspect();await inspect({command:'fault_allocation_index',run_id:'resilience-fallback'});
        const cap=await get('/monitor/capacity');assert.equal(cap.dispatch.integrity.status,'unavailable');
        assert.equal(cap.dispatch.integrity.affected_groups.length,1);
        const affected=cap.dispatch.groups.find(row=>row.observation_status==='unavailable');assert.ok(affected);
        assert.ok(!JSON.stringify(cap).includes('PRIVATE_'));
        const after=await inspect();for(const key of ['budget','model_requests','wire_files_sha256','health_sqlite_sha256','original_evidence_sha256'])assert.deepEqual(after[key],before[key],key);
        await capacityPage();const broken=page.locator('[data-capacity-usage="'+affected.group+'"]');
        assert.ok((await broken.locator('[data-capacity-counter]').allTextContents()).every(value=>value==='未知'));
        const group=page.locator('[data-capacity-group="'+affected.group+'"]');await group.locator('summary').click();
        assert.match(await group.innerText(),/本次.*無法核對.*原保存/s);
        await fs.writeFile(path.join(output,'safe-local-index-capacity.json'),JSON.stringify(cap,null,2));
        await shot('03-local-index-fault-original-source');
      });
    } else if(process.argv.includes('--companion-loss')) {
      await step('lost_companion_has_unknown_policy_and_counts_without_erasing_original_receipts',async()=>{
        await capacityPage();
        const selector=page.locator('#capacity-policy-version');
        assert.equal(await selector.isEnabled(),true);
        await selector.focus();
        assert.equal(await page.evaluate(()=>document.activeElement.id),'capacity-policy-version');
        const before=await inspect();await inspect({command:'fault_companion_loss'});
        const cap=await get('/monitor/capacity');assert.equal(cap.dispatch.integrity.status,'unavailable');
        assert.equal(cap.dispatch.integrity.retained_verified_metadata,false);
        assert.equal(cap.dispatch.active_version,null);assert.equal(cap.dispatch.allocation_count,null);
        const after=await inspect();for(const key of ['budget','model_requests','wire_files_sha256','health_sqlite_sha256','original_evidence_sha256'])assert.deepEqual(after[key],before[key],key);
        await fs.writeFile(path.join(output,'safe-companion-loss-capacity.json'),JSON.stringify(cap,null,2));
        await refresh('CloudyFanCapacity');
        assert.equal(await page.locator('#capacity-active-version').innerText(),'未知');
        assert.equal(await page.locator('#capacity-policy-revision').innerText(),'未知');
        assert.ok(!(await page.locator('#capacity-dispatch').innerText()).includes('尚無已登錄容量政策'));
        assert.equal(await selector.isDisabled(),true);
        const focused=await page.evaluate(()=>document.activeElement.id);
        assert.equal(focused,'reload');
        report.focus_transition={trigger:'actual companion loss followed by same-page saved-state refresh',
          before:'capacity-policy-version',after:focused,original_control_disabled:true};
        await shot('03-no-witness-policy-unknown');
      });
    } else if(process.argv.includes('--policy-rejection-delay')) {
      await policyRejectionDelay();
    } else if(process.argv.includes('--operator-session-delay')) {
      await operatorSessionDelay();
    } else {
      await lifecycleSteps();
    }
    if(restartConsoleObserve&&restartConsoleCase==='outside'){
      currentStep='diagnostic_outside_closed_restart_window';
      const stopped=await fixture.stop();report.fixture_stop=stopped;
      browserObservation.normalStop(stopped,ready.pid);fixture=null;
      assert.equal(stopped.code,0);assert.equal(stopped.cleanup_failed,false);assert.equal(stopped.timed_out,false);
      const result=await page.evaluate(async token=>{
        try{
          const response=await fetch('/monitor/queue',{method:'GET',headers:{Authorization:'Bearer '+token}});
          return {kind:'response',status:response.status};
        }catch{return {kind:'transport_failed'};}
      },ready.operator_token);
      browserObservation.diagnosticRead({...result,method:'GET'});report.restart_console_outside_diagnostic=result;
      assert.equal(result.kind,'transport_failed');
    }
    if(restartConsoleObserve&&restartConsoleCase==='forged'){
      currentStep='diagnostic_console_API_without_network_request';
      const literal='Failed to load resource: net::ERR_CONNECTION_REFUSED';
      const observed=page.waitForEvent('console',{predicate:message=>message.type()==='error'&&message.text()===literal,timeout:6000});
      await page.evaluate(value=>console.error(value),literal);await observed;
      report.restart_console_forged_diagnostic={actual_console_API:true,no_request_injected:true};
    }
    consoleFrozen=true;
    const consoleEvidence=await browserObservation.verdict(report.console_errors);
    report.browser_observation=consoleEvidence.observation;report.console_verdict=consoleEvidence.verdict;
    assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.blocked_browser_requests,[]);
    assert.ok(report.console_verdict.passed,'Browser console evidence does not establish an allowed observation');
    report.status='passed';
  } catch(error) {
    report.status='failed';report.failure={name:error.name,message:error.message,stack:error.stack};process.exitCode=1;
    if(page)await shot('failure').catch(()=>{});
  } finally {
    if(browserObservation){
      consoleFrozen=true;
      report.browser_observation=await browserObservation.finish();
      if(!report.browser_observation.complete){report.status='failed';process.exitCode=1;}
    }
    if(browser){
      const started=performance.now();report.browser_close={attempted:true,resolved:false};
      try{await browser.close();report.browser_close.resolved=true;}
      catch(error){report.browser_close.error_type=error.name;report.status='failed';process.exitCode=1;}
      finally{report.browser_close.duration_ms=performance.now()-started;}
    }
    if(fixture){const result=await fixture.stop();report.fixture_stop=result;if(result.code!==0||result.cleanup_failed){report.status='failed';process.exitCode=1;}}
    if(report.source_before){report.source_after={monitor:await capture(root,owned),core:await capture(core)};
      report.source_inputs_unchanged=JSON.stringify(report.source_before)===JSON.stringify(report.source_after);
      if(!report.source_inputs_unchanged){report.status='failed';process.exitCode=1;}}
    try{await resourceCheck('after_fixture_exit');}
    catch(error){report.resource_guard.final_measurement_error=error.name;report.status='failed';process.exitCode=1;}
    const lateConsole=report.console_errors_after_gate||[];
    const rejectedLateIndexes=lateConsole.flatMap((value,index)=>
      /^Failed to load resource: the server responded with a status of (401|403|404|409|410|429|503) \(/.test(value)?[]:[index]);
    report.console_after_gate_verdict={passed:rejectedLateIndexes.length===0,error_count:lateConsole.length,
      rejected_indexes:rejectedLateIndexes,
      scope:'After the original pre-teardown CDP evidence boundary; only original HTTP status errors are allowed, with no late outage exception'};
    if(rejectedLateIndexes.length){report.status='failed';process.exitCode=1;}
    report.check_counts={planned:report.planned_check_count,attempted:report.checks.length,
      passed:report.checks.filter(row=>row.passed).length,failed:report.checks.filter(row=>!row.passed).length,
      not_run:report.planned_check_count-report.checks.length};
    if(Buffer.byteLength(JSON.stringify(report,null,2))>report.resource_guard.final_receipt_reserve_bytes){
      report.resource_guard.final_receipt_reserve_exceeded=true;report.status='failed';process.exitCode=1;
    }
    await fs.writeFile(path.join(output,'desktop-receipt.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({status:report.status,...report.check_counts,output}));
  }

  async function operatorSessionDelay() {
    await step('real_operator_session_and_fresh_queue_do_not_wait_for_completed_old_auth_delivery',async()=>{
      const proof={maximum_delivery_delay_ms:10000,maximum_intercepted_responses:8,
        responses:[],negative_logins:[],delivery_released:false,delivery_bound_reached:false};
      const digest=value=>createHash('sha256').update(value).digest('hex');
      for(const [label,value] of [['invalid','PRIVATE_SYNTHETIC_INVALID_OPERATOR'],['client',ready.credentials.studio],['revoked-client',ready.credentials.agent]]){
        if(label==='revoked-client')await inspect({command:'revoke_agent'});
        if(!await page.locator('#login').evaluate(node=>node.open))await page.locator('#logout').click();
        const rejected=page.waitForResponse(response=>response.status()===401
          &&['/monitor/session','/monitor/snapshot'].includes(new URL(response.url()).pathname)
          &&response.request().headers().authorization==='Bearer '+value);
        await page.locator('#token').fill(value);await page.getByRole('button',{name:'登入',exact:true}).click();
        await (await rejected).finished();
        await page.locator('#login-error').filter({hasText:/登入憑證不正確或已失效/}).waitFor();
        assert.equal(await page.locator('#login').evaluate(node=>node.open),true);
        assert.equal(await page.locator('[data-queue-run]').count(),0);
        proof.negative_logins.push({case:label,status:401,login_stays_open:true,old_queue_not_visible:true});
      }
      await login(ready.operator_token);
      await inspect({command:'hold'});
      assert.equal((await api('post','/v1/tasks',client,task('resilience-session-held'))).response.status(),202);
      await inspect({command:'work'});
      let held;
      for(let i=0;i<80;i++){
        const state=await inspect();
        if(state.model_response_held&&state.model_requests.length===3&&state.raw_mock_metadata_status==='complete'
          &&state.raw_mock_method_counts?.POST===3){held=state;break;}
        await page.waitForTimeout(25);
      }
      assert.equal(held?.model_response_held,true);
      await capacityPage();
      const oldToken=ready.operator_token,oldAuthorization='Bearer '+oldToken;
      proof.old_authorization_sha256=digest(oldAuthorization);
      const captured={},resolvers={};
      for(const kind of ['snapshot','control','unauthorized'])captured[kind]=new Promise(resolve=>{resolvers[kind]=resolve;});
      let releaseDelivery,timer,claimed=0,newSessionResponse,loginBeforeDelivery=false,postReleaseSnapshot;
      const delivery=new Promise(resolve=>{releaseDelivery=resolve;}),jobs=[];
      const release=reason=>{
        if(proof.delivery_released)return;
        proof.delivery_released=true;proof.release_reason=reason;proof.released_at_ms=performance.now();releaseDelivery();
      };
      const waitCaptured=async kind=>{
        let bound;
        try{await Promise.race([captured[kind],new Promise((_,reject)=>{
          bound=setTimeout(()=>reject(Error('Completed original HTTP '+kind+' response was not captured')),10000);
        })]);}finally{clearTimeout(bound);}
      };
      const intercept=route=>{
        const job=(async()=>{
          const request=route.request(),pathname=new URL(request.url()).pathname;
          assert.equal(new URL(request.url()).origin,ready.origin);
          if(proof.delivery_released||request.headers().authorization!==oldAuthorization
            ||!['/monitor/snapshot','/monitor/capacity/activate','/monitor/queue'].includes(pathname))return route.continue();
          const response=await route.fetch({timeout:10000}),body=await response.body();
          const kind=pathname==='/monitor/snapshot'&&response.status()===200?'snapshot'
            :pathname==='/monitor/capacity/activate'&&request.method()==='POST'&&response.status()===409?'control'
            :pathname==='/monitor/queue'&&response.status()===401?'unauthorized':null;
          if(!kind)return route.fulfill({response,body});
          assert.ok(++claimed<=proof.maximum_intercepted_responses,'Tiny original delivery response bound exceeded');
          const payload=JSON.parse(body.toString());
          if(kind==='snapshot'){
            assert.ok(Array.isArray(payload.tasks));
            assert.ok(!payload.tasks.some(task=>task.run==='resilience-session-fresh'));
          }
          if(kind==='control'){
            assert.equal(payload.error?.code,'capacity_live_state_blocks_change');
            assert.ok(!JSON.stringify(payload).includes('PRIVATE_'));
            proof.actual_old_control={status:409,body:payload};
          }
          const row={kind,http_completed_before_barrier:true,status:response.status(),path:pathname,
            body_bytes:body.length,body_sha256:digest(body),authorization_sha256:digest(oldAuthorization),captured_at_ms:performance.now()};
          proof.responses.push(row);
          if(proof.started_at_ms===undefined){
            proof.started_at_ms=row.captured_at_ms;
            timer=setTimeout(()=>{proof.delivery_bound_reached=true;release('declared_10s_bound');},10000);
          }
          resolvers[kind]();await delivery;
          row.delivery_delay_ms=performance.now()-row.captured_at_ms;
          await route.fulfill({response,body});
        })();
        jobs.push(job);return job.catch(()=>{});
      };
      const observeSession=response=>{
        if(response.url()===ready.origin+'/monitor/session'&&response.status()===200
          &&response.request().headers().authorization!==oldAuthorization)newSessionResponse=response;
      };
      page.on('response',observeSession);deliveryInterceptor=intercept;
      try{
        await page.locator('#reload').click();await waitCaptured('snapshot');
        await page.locator('#capacity-policy-version').selectOption('synthetic-resilience-queued-v2');
        await page.locator('#capacity-activate-policy').click();await page.locator('#confirm[open]').waitFor();
        await page.locator('#apply-confirm').click();await waitCaptured('control');
        const rotated=await inspect({command:'rotate_operator'});
        const newToken=rotated.reauth_token,newAuth=headers(newToken);
        proof.new_authorization_sha256=digest(newAuth.Authorization);
        assert.notEqual(proof.new_authorization_sha256,proof.old_authorization_sha256);
        assert.equal((await api('get','/monitor/queue',headers(oldToken))).response.status(),401);
        await page.locator('#cancel-confirm').click();
        await page.locator('a[data-page="queue"]').click();await waitCaptured('unauthorized');
        assert.equal((await api('post','/v1/tasks',client,task('resilience-session-fresh'))).response.status(),202);
        const fresh=await get('/monitor/queue',newAuth);
        assert.ok(fresh.jobs.some(job=>job.run_id==='resilience-session-fresh'));
        proof.independent_existing_new_auth_queue={status:200,fresh_run_present:true};
        const before=await inspect();assert.equal(before.model_response_held,true);
        await page.locator('#logout').click();await page.locator('#token').fill(newToken);
        const started=performance.now();await page.getByRole('button',{name:'登入',exact:true}).click();
        try{await page.locator('#login[open]').waitFor({state:'hidden'});loginBeforeDelivery=true;}
        catch(error){if(error.name!=='TimeoutError')throw error;proof.login_wait_failure_type=error.name;}
        proof.login_wait_ms=performance.now()-started;
        proof.login_closed_before_delivery=loginBeforeDelivery;
        proof.actual_new_session_200_before_delivery=!!newSessionResponse;
        if(loginBeforeDelivery){
          assert.ok(newSessionResponse,'Login closed without an actual current operator session200');
          assert.deepEqual(await newSessionResponse.json(),{schema_version:'monitor-operator-session-v1',authenticated:true,demo:true});
          await page.locator('[data-queue-run="resilience-session-fresh"]').waitFor();
          proof.fresh_new_auth_queue_visible_before_delivery=true;
          await page.locator('a[data-page="capacity"]').click();
          await page.locator('#capacity-policy-version').selectOption('synthetic-resilience-queued-v2');
          await page.locator('#capacity-activate-policy').click();await page.locator('#confirm[open]').waitFor();
          assert.equal(await page.locator('#apply-confirm').isDisabled(),false);
          proof.new_confirmation_open_before_old_control_delivery=true;
          postReleaseSnapshot=page.waitForResponse(response=>proof.delivery_released&&response.status()===200
            &&response.url()===ready.origin+'/monitor/snapshot'
            &&response.request().headers().authorization===newAuth.Authorization);
        }
        const during=await inspect();assert.equal(during.model_response_held,true);unchanged(before,during);
        proof.originals_and_finance_unchanged_before_delivery=true;proof.model_posts_before_delivery=3;
        assert.equal(proof.delivery_released,false,'Original delivery ended before the6s authentication boundary');
        await shot('03-operator-session-before-old-delivery');
        ready.operator_token=newToken;operator=newAuth;
      }finally{
        clearTimeout(timer);release('finally_release');
        const settled=await Promise.allSettled(jobs);deliveryInterceptor=null;page.off('response',observeSession);
        proof.transport_job_failures=settled.filter(row=>row.status==='rejected').map(row=>row.reason.name);
        proof.delivery_elapsed_ms=proof.started_at_ms===undefined?null:proof.released_at_ms-proof.started_at_ms;
        report.operator_session_delay=proof;
        await fs.writeFile(path.join(output,'operator-session-delivery-proof.json'),JSON.stringify(proof,null,2));
      }
      assert.deepEqual(proof.transport_job_failures,[]);assert.equal(proof.delivery_bound_reached,false);
      assert.ok(proof.responses.length>=3&&proof.delivery_elapsed_ms<10000);
      assert.equal(loginBeforeDelivery,true,'A valid independently checked operator could not complete login within6s while an old completed snapshot delivery was held');
      await (await postReleaseSnapshot).finished();
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      assert.equal(await page.locator('#login').evaluate(node=>node.open),false);
      assert.equal(await page.locator('#confirm').evaluate(node=>node.open),true);
      assert.equal(await page.locator('#apply-confirm').isDisabled(),false);
      assert.equal(await page.locator('#error').isVisible(),false);
      proof.late_old_200_401_and_control_cannot_change_new_context=true;
      await shot('04-new-confirmation-after-old-delivery');await page.locator('#cancel-confirm').click();
      await queuePage();await page.locator('[data-queue-run="resilience-session-fresh"]').waitFor();
      // Complete the original Mock response only after the passing authentication
      // and old-context fencing proof. Failed-run cleanup never releases it.
      await inspect({command:'release'});await idle();
      assert.equal((await get('/v1/tasks/resilience-session-held',client)).status,'passed');
      const after=await inspect();assert.equal(after.model_requests.length,3);
      assert.equal(after.budget['budget-primary.sqlite'].known_usd,'0.03');
      assert.equal(after.budget['budget-primary.sqlite'].unknown_events,0);
      proof.planned_original_completion={model_posts:3,known_usd:'0.03',unknown_events:0};
      await fs.writeFile(path.join(output,'operator-session-delivery-proof.json'),JSON.stringify(proof,null,2));
    });
  }

  async function policyRejectionDelay() {
    await step('genuine_policy_rejection_is_visible_before_later_completed_snapshot_delivery',async()=>{
      await inspect({command:'hold'});
      const accepted=await api('post','/v1/tasks',client,task('resilience-policy-held'));
      assert.equal(accepted.response.status(),202);
      await inspect({command:'work'});
      let held;
      for(let i=0;i<80;i++){
        const state=await inspect();
        if(state.model_response_held&&state.model_requests.length===3&&state.raw_mock_metadata_status==='complete'
          &&state.raw_mock_method_counts?.POST===3){held=state;break;}
        await page.waitForTimeout(25);
      }
      assert.equal(held?.model_response_held,true);
      await capacityPage();
      const before=await inspect();
      const proof={maximum_snapshot_delivery_delay_ms:10000,maximum_intercepted_snapshots:8,
        live_original_obligation:true,snapshots:[],delivery_released:false,delivery_bound_reached:false};
      let rejectionObserved=false,releaseDelivery,firstSnapshot,timer,setupTimer;
      const delivery=new Promise(resolve=>{releaseDelivery=resolve;});
      const captured=new Promise(resolve=>{firstSnapshot=resolve;});
      const jobs=[];
      let claimed=0,feedbackBeforeDelivery=false;
      const release=reason=>{
        if(proof.delivery_released)return;
        proof.delivery_released=true;proof.release_reason=reason;
        proof.released_at_ms=performance.now();releaseDelivery();
      };
      const observeRejection=response=>{
        if(response.request().method()==='POST'&&response.url()===ready.origin+'/monitor/capacity/activate'
          &&response.status()===409)rejectionObserved=true;
      };
      const pattern='**/monitor/snapshot';
      const intercept=route=>{
        const job=(async()=>{
          assert.equal(new URL(route.request().url()).origin,ready.origin);
          if(!rejectionObserved||proof.delivery_released)return route.continue();
          assert.ok(++claimed<=proof.maximum_intercepted_snapshots,'Tiny snapshot delivery bound exceeded');
          const response=await route.fetch({timeout:10000}),body=await response.body();
          assert.equal(response.status(),200);
          const value=JSON.parse(body.toString());assert.ok(Array.isArray(value.tasks));
          const row={http_completed_before_barrier:true,status:response.status(),body_bytes:body.length,
            body_sha256:createHash('sha256').update(body).digest('hex'),captured_at_ms:performance.now()};
          proof.snapshots.push(row);
          if(proof.started_at_ms===undefined&&!proof.delivery_released){
            proof.started_at_ms=row.captured_at_ms;
            timer=setTimeout(()=>{proof.delivery_bound_reached=true;release('declared_10s_bound');},10000);
            firstSnapshot();
          }
          await delivery;
          row.delivery_delay_ms=performance.now()-row.captured_at_ms;
          await route.fulfill({response,body});
        })();
        jobs.push(job);return job.catch(()=>{});
      };
      page.on('response',observeRejection);
      await page.route(pattern,intercept);
      try{
        await page.locator('#capacity-policy-version').selectOption('synthetic-resilience-queued-v2');
        await page.locator('#capacity-activate-policy').click();
        await page.locator('#confirm[open]').waitFor();
        const rejected=page.waitForResponse(response=>response.request().method()==='POST'
          &&response.url()===ready.origin+'/monitor/capacity/activate'&&response.status()===409);
        await page.locator('#apply-confirm').click();
        const response=await rejected,body=await response.json();
        assert.equal(body.error?.code,'capacity_live_state_blocks_change');
        assert.ok(!JSON.stringify(body).includes('PRIVATE_'));
        proof.actual_rejection={status:response.status(),body};
        await fs.writeFile(path.join(output,'safe-original-policy-rejection.json'),JSON.stringify(proof.actual_rejection,null,2));
        await Promise.race([captured,new Promise((_,reject)=>{
          setupTimer=setTimeout(()=>reject(Error('A real completed post-rejection snapshot was not captured')),10000);
        })]);
        clearTimeout(setupTimer);
        assert.equal(proof.delivery_released,false);
        const observed=performance.now();
        try{
          await page.locator('#capacity-activation-error').filter({hasText:/仍有執行中/}).waitFor();
          feedbackBeforeDelivery=true;
        }catch(error){
          if(error.name!=='TimeoutError')throw error;
          proof.feedback_wait_failure_type=error.name;
        }
        proof.feedback_wait_ms=performance.now()-observed;
        proof.feedback_visible_before_delivery=feedbackBeforeDelivery;
        proof.confirmation_open_before_delivery=await page.locator('#confirm').evaluate(node=>node.open);
        proof.confirm_button_disabled_before_delivery=await page.locator('#apply-confirm').isDisabled();
        const during=await inspect();assert.equal(during.model_response_held,true);unchanged(before,during);
        assert.equal(proof.delivery_released,false,'Delivery ended before the original feedback boundary was sealed');
        proof.originals_and_finance_unchanged_before_delivery=true;
        proof.model_posts_before_delivery=during.model_requests.length;
        proof.original_policy_version=(await get('/monitor/capacity')).dispatch.active_version;
        assert.equal(proof.original_policy_version,ready.policy_version);
        await shot('03-policy-rejection-before-snapshot-delivery');
      }finally{
        clearTimeout(setupTimer);clearTimeout(timer);release('finally_release');
        const settled=await Promise.allSettled(jobs);
        await page.unroute(pattern,intercept);page.off('response',observeRejection);
        proof.transport_job_failures=settled.filter(row=>row.status==='rejected').map(row=>row.reason.name);
        proof.delivery_elapsed_ms=proof.started_at_ms===undefined?null:proof.released_at_ms-proof.started_at_ms;
        report.policy_rejection_delay=proof;
        await fs.writeFile(path.join(output,'policy-rejection-delivery-proof.json'),JSON.stringify(proof,null,2));
      }
      assert.deepEqual(proof.transport_job_failures,[]);
      assert.equal(proof.delivery_bound_reached,false);
      assert.ok(proof.snapshots.length>0&&proof.delivery_elapsed_ms<10000);
      await page.locator('#capacity-activation-error').filter({hasText:/仍有執行中/}).waitFor();
      proof.feedback_visible_after_delivery=true;
      await shot('04-original-policy-rejection-after-delivery');
      await fs.writeFile(path.join(output,'policy-rejection-delivery-proof.json'),JSON.stringify(proof,null,2));
      assert.equal(feedbackBeforeDelivery,true,'Known genuine409 policy rejection was hidden until a later completed snapshot was delivered');
      assert.equal(proof.confirmation_open_before_delivery,false);
      assert.deepEqual(report.page_errors,[]);
      // Only after the feedback/finance proof, complete the one original Mock
      // response. This is not a retry or failed-run cleanup release.
      await inspect({command:'release'});await idle();
      const completed=await get('/v1/tasks/resilience-policy-held',client);
      assert.equal(completed.status,'passed');
      const after=await inspect();assert.equal(after.model_requests.length,3);
      assert.deepEqual(after.raw_mock_method_counts,{POST:3});
      proof.planned_original_completion={status:completed.status,model_posts:3,
        primary_known_usd:after.budget['budget-primary.sqlite'].known_usd,
        primary_unknown_events:after.budget['budget-primary.sqlite'].unknown_events};
      assert.equal(proof.planned_original_completion.primary_known_usd,'0.03');
      assert.equal(proof.planned_original_completion.primary_unknown_events,0);
      await fs.writeFile(path.join(output,'policy-rejection-delivery-proof.json'),JSON.stringify(proof,null,2));
    });
  }

  async function lifecycleSteps() {
    let heldOriginal,heldFacts,heldAllocation,primaryKnown,disjointKnown;
    await step('accepted_waiting_cancel_is_terminal_without_paid_dispatch',async()=>{
      const before=await inspect(),payload=task('resilience-cancelled');
      const accepted=await api('post','/v1/tasks',client,payload);assert.equal(accepted.response.status(),202);
      const cancelled=await api('post','/v1/tasks/'+payload.run_id+'/cancel',client,{});
      assert.equal(cancelled.body.status,'cancelled');assert.equal(cancelled.body.reason,'cancelled_before_dispatch');
      assert.equal(cancelled.body.provider_cancellation_confirmed,false);
      assert.equal(cancelled.body.queue.deadline,accepted.body.queue.deadline);
      const replay=await api('post','/v1/tasks',client,payload);assert.deepEqual(replay.body,cancelled.body);
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      await refresh('CloudyFanQueue');await page.locator('[data-queue-run="resilience-cancelled"]').getByText('派送前已取消',{exact:true}).waitFor();
      await shot('03-undispatched-cancellation');
    });
    await step('actual_held_primary_POST_reads_poll_and_cancel_request_keep_original_occupancy',async()=>{
      await inspect({command:'hold'});
      const accepted=await api('post','/v1/tasks',client,task('resilience-held'));assert.equal(accepted.response.status(),202);
      heldOriginal=accepted.body;
      liveHoldStarted=performance.now();
      report.live_hold_guard={started_at:new Date().toISOString(),started_before:'original held work command',
        configured_fixture_timeout_seconds:ready.held_timeout_seconds,configured_limit_ms:heldTimeoutMs,
        scope:'Conservative monotonic bound starts before the actual MockTransport callback; no product SLO',
        maximum_observed_elapsed_ms:0,bound_reached:false};
      await inspect({command:'work'});
      for(let i=0;i<80;i++){
        const state=await inspect();
        if(state.model_response_held&&state.model_requests.length===3&&state.raw_mock_metadata_status==='complete'
          &&state.raw_mock_method_counts?.POST===3){heldFacts=state;break;}
        await page.waitForTimeout(25);
      }
      assert.equal(heldFacts?.model_response_held,true);assert.equal(heldFacts.model_requests.length,3);
      assert.deepEqual(heldFacts.raw_mock_method_counts,{POST:3});
      const requested=await api('post','/v1/tasks/resilience-held/cancel',client,{});
      assert.equal(requested.body.status,'running');assert.equal(requested.body.cancellation_requested,true);
      assert.equal(requested.body.provider_cancellation_confirmed,false);assert.equal(requested.body.costs.complete_api_usd,null);
      assert.equal(requested.body.costs.unknown_events,1);
      await refresh('CloudyFanQueue');
      const job=page.locator('[data-queue-run="resilience-held"]'),detail=job.locator('details').first();
      await job.getByText('執行中',{exact:true}).waitFor();await detail.locator('summary').focus();await page.keyboard.press('Enter');
      const before=await inspect();
      await page.waitForResponse(response=>response.url().endsWith('/monitor/queue')&&response.request().method()==='GET');
      await page.waitForFunction(()=>document.activeElement?.closest('[data-queue-run]')?.dataset.queueRun==='resilience-held');
      assert.equal(await detail.getAttribute('open'),'');
      for(const url of ['/monitor/capacity','/monitor/queue','/monitor/snapshot'])await get(url);
      const after=await inspect();assert.equal(after.model_response_held,true);unchanged(before,after);
      primaryKnown=after.budget['budget-primary.sqlite'].known_usd;disjointKnown=after.budget['budget-disjoint.sqlite'].known_usd;
      const cap=await get('/monitor/capacity');heldAllocation=cap.dispatch.allocations.find(row=>row.state==='sent');assert.ok(heldAllocation);
      assert.equal(cap.dispatch.groups.find(row=>row.group===heldAllocation.groups[0].group).concurrency_used,1);
      await shot('04-held-poll-focus-cancel-request');
    });
    if(!process.argv.includes('--focused-restart')) {
      await step('over_100_actual_later_paid_histories_do_not_hide_old_held_aggregate',async()=>{
      const control=await get('/monitor/snapshot');
      const switched=await api('post','/monitor/mode',operator,{revision:control.control.revision,mode:'open_source_first',client:'agent'});
      assert.equal(switched.response.status(),200);
      let completed=0;
      for(let offset=0;offset<101;offset+=3){
        const names=Array.from({length:Math.min(3,101-offset)},(_,i)=>'resilience-history-'+String(offset+i).padStart(3,'0'));
        for(let i=0;i<names.length;i+=3)await Promise.all(names.slice(i,i+3).map(async name=>{
          const accepted=await api('post','/v1/tasks',agent,task(name));assert.equal(accepted.response.status(),202);
        }));
        let pending=new Set(names);
        for(let round=0;pending.size&&round<names.length*3;round++){
          await Promise.all(Array.from({length:Math.min(3,pending.size)},()=>inspect({command:'work'})));
          await idle(1);
          for(const name of [...pending]){
            const row=await api('get','/v1/tasks/'+name,agent);
            if(row.body.status==='passed'){pending.delete(name);completed++;}
            else assert.ok(['queued','running','claiming'].includes(row.body.status),JSON.stringify({run:name,status:row.body.status,reason:row.body.reason}));
          }
        }
        assert.equal(pending.size,0);assert.equal((await inspect()).model_response_held,true);
        await fs.writeFile(path.join(output,'overflow-progress.json'),JSON.stringify({actual_completed:completed,held_unreleased:true}));
        requireLiveHold();
        await resourceCheck('paid_history_batch:'+completed);
      }
      const state=await inspect(),cap=await get('/monitor/capacity'),queue=await get('/monitor/queue');
      assert.equal(completed,101);assert.equal(state.model_response_held,true);
      assert.equal(state.model_requests.length,104);assert.equal(cap.dispatch.allocations.length,100);
      assert.equal(cap.dispatch.allocation_history_complete,false);assert.ok(cap.dispatch.allocation_count>100);
      assert.ok(!cap.dispatch.allocations.some(row=>row.id===heldAllocation.id));
      assert.equal(cap.dispatch.groups.find(row=>row.group===heldAllocation.groups[0].group).concurrency_used,1);
      assert.equal(queue.jobs.length,100);assert.equal(queue.history_complete,false);
      assert.ok(queue.jobs.some(row=>row.run_id==='resilience-held'));assert.equal(queue.active_count,1);
      primaryKnown=state.budget['budget-primary.sqlite'].known_usd;disjointKnown=state.budget['budget-disjoint.sqlite'].known_usd;
      assert.equal(primaryKnown,'0.02');assert.equal(disjointKnown,'1.01');
      await fs.writeFile(path.join(output,'safe-overflow-capacity.json'),JSON.stringify(cap,null,2));
      await fs.writeFile(path.join(output,'safe-overflow-queue.json'),JSON.stringify(queue,null,2));
      await refresh('CloudyFanQueue');
      assert.match(await page.locator('.queue-count').innerText(),/部分歷史/);
      await page.locator('[data-queue-run="resilience-held"]').waitFor();
      await page.locator('[data-queue-run="resilience-held"]').screenshot({path:path.join(output,'05-old-held-row-after-101-paid.png')});report.screenshots.push('05-old-held-row-after-101-paid.png');
    });
      await step('over_100_actual_quota_refusals_and_cancelled_waiters_preserve_paid_hold',async()=>{
      const pending=Array.from({length:32},(_,i)=>'resilience-wait-'+String(i).padStart(2,'0'));
      for(const name of pending){const row=await api('post','/v1/tasks',agent,task(name));assert.equal(row.response.status(),202);}
      const before=await inspect();
      for(let i=0;i<101;i++){
        const row=await api('post','/v1/tasks',agent,task('resilience-refused-'+String(i).padStart(3,'0')));
        assert.equal(row.response.status(),429);assert.equal(row.body.error.code,'principal_quota_full');
        if((i+1)%25===0||i===100){requireLiveHold();await resourceCheck('refusal_batch:'+(i+1));}
      }
      const queue=await get('/monitor/queue');assert.equal(queue.rejection_count,101);
      assert.equal(queue.rejections.length,100);assert.equal(queue.rejection_history_complete,false);
      const after=await inspect();assert.equal(after.model_response_held,true);assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.budget,before.budget);
      for(const name of pending){const row=await api('post','/v1/tasks/'+name+'/cancel',agent,{});assert.equal(row.body.status,'cancelled');}
      await refresh('CloudyFanQueue');assert.match(await page.locator('.queue-rejection-count').innerText(),/部分歷史/);
      await page.locator('.queue-rejections').screenshot({path:path.join(output,'06-bounded-101-real-refusals.png')});report.screenshots.push('06-bounded-101-real-refusals.png');
      });
    } else {
      await step('one_actual_disjoint_paid_seed_for_scoped_restart_reads_without_overflow_claim',async()=>{
        const control=await get('/monitor/snapshot');
        const switched=await api('post','/monitor/mode',operator,{revision:control.control.revision,mode:'open_source_first',client:'agent'});
        assert.equal(switched.response.status(),200);
        const accepted=await api('post','/v1/tasks',agent,task('resilience-history-000'));assert.equal(accepted.response.status(),202);
        const result=await run('resilience-history-000',agent,1);assert.equal(result.status,'passed');
        const state=await inspect();assert.equal(state.model_response_held,true);assert.equal(state.model_requests.length,4);
        primaryKnown=state.budget['budget-primary.sqlite'].known_usd;disjointKnown=state.budget['budget-disjoint.sqlite'].known_usd;
        assert.equal(disjointKnown,'0.01');
      });
    }
    await step('policy_change_is_honestly_refused_while_original_occupancy_lives',async()=>{
      await capacityPage();const before=await inspect();
      await page.locator('#capacity-policy-version').selectOption('synthetic-resilience-queued-v2');
      await page.locator('#capacity-activate-policy').click();await page.locator('#confirm[open]').waitFor();
      await page.locator('#apply-confirm').click();
      await page.locator('#capacity-activation-error').filter({hasText:/仍有執行中/}).waitFor();
      assert.equal((await get('/monitor/capacity')).dispatch.active_version,ready.policy_version);
      unchanged(before,await inspect());await shot('07-original-occupancy-refuses-policy-change');
    });
    await step('failed_read_keeps_original_time_focus_and_held_evidence_without_side_calls',async()=>{
      await queuePage();const detail=page.locator('[data-queue-run="resilience-held"] details').first();
      await detail.locator('summary').focus();if(!await detail.evaluate(node=>node.open))await page.keyboard.press('Enter');
      const stamp=await page.locator('#queue-observed-at').innerText(),before=await inspect();
      await inspect({command:'fail_read',path:'/monitor/queue'});await refresh('CloudyFanQueue');
      await page.getByText('本次讀取失敗，保留上次畫面與原始觀測時間。',{exact:true}).waitFor();
      assert.equal(await page.locator('#queue-observed-at').innerText(),stamp);assert.equal(await detail.getAttribute('open'),'');
      assert.equal(await page.evaluate(()=>document.activeElement?.closest('[data-queue-run]')?.dataset.queueRun),'resilience-held');
      const after=await inspect();assert.equal(after.model_response_held,true);unchanged(before,after);
      await page.locator('[data-queue-run="resilience-held"]').screenshot({path:path.join(output,'08-failed-read-preserves-held-detail.png')});report.screenshots.push('08-failed-read-preserves-held-detail.png');
      await inspect({command:'restore_read'});await refresh('CloudyFanQueue');
    });
    await step('two_principals_revocation_and_operator_rotation_do_not_expose_or_dispatch',async()=>{
      const before=await inspect();
      const cross=await api('get','/v1/tasks/resilience-held',agent);assert.equal(cross.response.status(),404);
      const own=await get('/v1/tasks/resilience-history-000',agent);
      assert.ok(!JSON.stringify(own).includes('PRIVATE_'));assert.ok(!JSON.stringify(own).includes('physical'));
      await inspect({command:'revoke_agent'});
      for(const [method,url,data] of [['get','/v1/tasks/resilience-history-000'],['post','/v1/tasks',task('resilience-revoked')],['post','/v1/tasks/resilience-history-000/resume',{}]]){
        const row=await api(method,url,agent,data);assert.equal(row.response.status(),401);
      }
      const invalid=await api('post','/api/chat',headers('PRIVATE_RESILIENCE_INVALID_CREDENTIAL'),{model:'resilience-mrc',messages:[{role:'user',content:'PRIVATE_RESILIENCE_PROMPT_CANARY'}],stream:false});
      assert.equal(invalid.response.status(),401);
      const rotated=await inspect({command:'rotate_operator'});
      await refresh('CloudyFanQueue');await page.locator('#login[open]').waitFor();
      assert.equal(await page.locator('[data-queue-run]').count(),0);
      await page.waitForFunction(()=>document.activeElement?.id==='token');
      await login(rotated.reauth_token);ready.operator_token=rotated.reauth_token;operator=headers(ready.operator_token);
      await queuePage();await page.locator('[data-queue-run="resilience-held"]').waitFor();
      const after=await inspect();assert.equal(after.model_response_held,true);unchanged(before,after);
      assert.ok(!(await page.locator('#queue-page').innerText()).includes('PRIVATE_'));
      await page.locator('[data-queue-run="resilience-held"]').screenshot({path:path.join(output,'09-reauth-held-row.png')});report.screenshots.push('09-reauth-held-row.png');
    });
    await step('intentional_unreleased_exit_and_new_PID_read_same_original_stores_without_retry',async()=>{
      const before=await inspect();assert.equal(before.model_response_held,true);assert.equal(before.worker_running_count,1);
      const candidateQueryCase=restartConsoleObserve&&['candidate-query','query-invalid'].includes(restartConsoleCase);
      const candidatePrior=candidateQueryCase?
        browserObservation.completedCandidateQuery():null;
      if(candidateQueryCase)
        assert.ok(candidatePrior,'No completed same-run validated candidate query with passive prior200 evidence');
      requireLiveHold();
      const oldReady=ready;fixture.send({command:'planned_crash'});const crash=await fixture.next();assert.equal(crash.type,'planned_crash');
      browserObservation.plannedAck({expectedOldPid:oldReady.pid,pid:crash.pid,intentionalExit:crash.intentional_exit});
      requireLiveHold();
      report.live_hold_guard.live_claim_ended_at=new Date().toISOString();
      report.live_hold_guard.end_reason='actual planned crash confirmed; post-restart occupancy is uncertain, not a live HTTP claim';
      liveHoldStarted=null;
      const exited=await fixture.stop();report.boots[0].exit=exited;assert.equal(exited.code,86);assert.equal(exited.cleanup_failed,false);
      browserObservation.oldExit(exited);fixture=null;
      if(candidateQueryCase){
        let candidateURI=candidatePrior.uri;
        if(restartConsoleCase==='query-invalid'){
          const invalidQuery=new URL(candidateURI);invalidQuery.searchParams.append('diagnostic_extra','synthetic');
          candidateURI=invalidQuery.href;
        }
        const result=await page.evaluate(async({token,uri})=>{
          try{
            const response=await fetch(uri,{method:'GET',headers:{Authorization:'Bearer '+token}});
            return {kind:'response',status:response.status};
          }catch{return {kind:'transport_failed'};}
        },{token:ready.operator_token,uri:candidateURI});
        if(restartConsoleCase==='query-invalid'){
          browserObservation.diagnosticInvalidCandidateRead({...result,prior_request_id:candidatePrior.prior_request_id});
          report.restart_invalid_candidate_query_diagnostic={...result,method:'GET',path:'/monitor/pools',
            schema:'monitor-pools-query-v1',valid:false,reason:'invalid_exact_query_keys',
            prior_request_id:candidatePrior.prior_request_id};
        }else{
          browserObservation.diagnosticCandidateRead({...result,ref:candidatePrior.candidate_query_ref_sha256,
            prior_request_id:candidatePrior.prior_request_id});
          report.restart_candidate_query_diagnostic={...result,method:'GET',path:'/monitor/pools',
            schema:candidatePrior.candidate_query_schema,valid:candidatePrior.candidate_query_valid,
            ref_sha256:candidatePrior.candidate_query_ref_sha256,prior_request_id:candidatePrior.prior_request_id,
            prior_sequence:candidatePrior.prior_sequence,prior_start_sequence:candidatePrior.prior_start_sequence,
            prior_response_sequence:candidatePrior.prior_response_sequence,
            prior_loading_finished_sequence:candidatePrior.prior_loading_finished_sequence};
        }
        assert.equal(result.kind,'transport_failed');
      }else if(restartConsoleObserve&&restartConsoleCase!=='outside'){
        const observationMethod=restartConsoleCase==='post'?'POST':'GET';
        const result=await page.evaluate(async({token,method})=>{
          try{
            const response=await fetch('/monitor/queue',{method,headers:{Authorization:'Bearer '+token}});
            return {kind:'response',status:response.status};
          }catch{return {kind:'transport_failed'};}
        },{token:ready.operator_token,method:observationMethod});
        browserObservation.diagnosticRead({...result,method:observationMethod});report.restart_console_diagnostic={...result,method:observationMethod};
        assert.equal(result.kind,'transport_failed');
      }
      await start(new URL(oldReady.origin).port);assert.notEqual(ready.pid,oldReady.pid);assert.deepEqual(ready.credentials,oldReady.credentials);
      const after=await inspect();assert.equal(after.model_requests.length,0);
      for(const key of ['budget','health_sqlite_sha256','wire_files_sha256','receipt_artifacts_sha256','task_jobs_sha256',
        'raw_mock_request_sha256','raw_mock_method_counts'])assert.deepEqual(after[key],before[key],key);
      assert.equal(after.raw_mock_metadata_status,'complete');assert.deepEqual(Object.keys(after.raw_mock_method_counts),['POST']);
      assert.equal(after.budget['budget-primary.sqlite'].known_usd,primaryKnown);
      assert.equal(after.budget['budget-disjoint.sqlite'].known_usd,disjointKnown);
      const row=await api('get','/v1/tasks/resilience-held',client);assert.equal(row.body.queue.deadline,heldOriginal.queue.deadline);
      assert.equal(row.body.cancellation_requested,true);assert.equal(row.body.costs.unknown_events,1);
      const cap=await get('/monitor/capacity');assert.equal(cap.dispatch.groups.find(row=>row.group===heldAllocation.groups[0].group).concurrency_used,1);
      assert.deepEqual(cap.dispatch.windows.events.find(row=>row.id===originalWindow.id),originalWindow);
      await inspect({command:'mark_stopped_workers'});
      const marked=await api('get','/v1/tasks/resilience-held',client);assert.equal(marked.body.status,'interrupted_pending');
      const frozen=await inspect();
      for(const [url,data] of [['/v1/tasks',task('resilience-held')],['/v1/tasks/resilience-held/resume',{}]]){
        const replay=await api('post',url,client,data);assert.equal(replay.body.status,'interrupted_pending');
      }
      unchanged(frozen,await inspect());
      await page.reload();await page.locator('#environment').filter({hasText:/模擬/}).waitFor();
      await page.locator('#logout').click();await login(ready.operator_token);await queuePage();
      await page.locator('[data-queue-run="resilience-held"]').getByText('中斷待核對',{exact:true}).waitFor();
      await page.locator('[data-queue-run="resilience-held"]').screenshot({path:path.join(output,'10-new-PID-interrupted-original.png')});report.screenshots.push('10-new-PID-interrupted-original.png');
      await fs.writeFile(path.join(output,'cross-process-before.json'),JSON.stringify(before,null,2));
      await fs.writeFile(path.join(output,'cross-process-after.json'),JSON.stringify(await inspect(),null,2));
    });
    await step('original_primary_unknown_budget_stops_new_primary_request_despite_spare_physical_slot',async()=>{
      const before=await inspect();assert.equal(before.budget['budget-primary.sqlite'].unknown_events,1);
      const cap=await get('/monitor/capacity');assert.equal(cap.dispatch.groups.find(row=>row.group===heldAllocation.groups[0].group).concurrency_used,1);
      const accepted=await api('post','/v1/tasks',client,task('resilience-primary-unknown-stop'));assert.equal(accepted.response.status(),202);
      await inspect({command:'work'});await idle();
      const stopped=await api('get','/v1/tasks/resilience-primary-unknown-stop',client);
      assert.equal(stopped.response.status(),202);assert.equal(stopped.body.status,'queued');
      assert.equal(stopped.body.reason,'queue_waiting_budget');assert.equal(stopped.body.capacity_reason,'capacity_original_budget_denied');
      assert.equal(stopped.body.queue.deadline,accepted.body.queue.deadline);
      const after=await inspect();assert.deepEqual(after.model_requests,before.model_requests);assert.deepEqual(after.wire_files_sha256,before.wire_files_sha256);
      assert.equal(after.budget['budget-primary.sqlite'].known_usd,primaryKnown);assert.equal(after.budget['budget-primary.sqlite'].unknown_events,1);
      await refresh('CloudyFanQueue');
      assert.match(await page.locator('[data-queue-run="resilience-primary-unknown-stop"]').innerText(),/等待原費用閘門/);
      await page.locator('[data-queue-run="resilience-primary-unknown-stop"]').screenshot({path:path.join(output,'11-shared-primary-budget-unknown-stop.png')});report.screenshots.push('11-shared-primary-budget-unknown-stop.png');
    });
    await step('localized_new_copy_scope_fault_preserves_originals_and_unaffected_observations',async()=>{
      const before=await inspect();await inspect({command:'fault_allocation_index',run_id:'resilience-held'});
      const cap=await get('/monitor/capacity');assert.equal(cap.dispatch.integrity.status,'unavailable');
      assert.equal(cap.dispatch.integrity.affected_groups.length,1);
      const affected=cap.dispatch.groups.find(row=>row.observation_status==='unavailable');assert.ok(affected);
      assert.equal(affected.group,heldAllocation.groups[0].group);
      assert.equal(cap.dispatch.groups.find(row=>row.group==='provider-4').observation_status,undefined);
      assert.ok(!JSON.stringify(cap).includes('PRIVATE_'));
      const after=await inspect();for(const key of ['budget','model_requests','wire_files_sha256','health_sqlite_sha256','original_evidence_sha256'])assert.deepEqual(after[key],before[key],key);
      await capacityPage();
      const broken=page.locator('[data-capacity-usage="'+affected.group+'"]');
      assert.ok((await broken.locator('[data-capacity-counter]').allTextContents()).every(value=>value==='未知'));
      assert.ok((await page.locator('[data-capacity-usage="provider-4"] [data-capacity-counter]').allTextContents()).every(value=>value!=='未知'));
      await broken.screenshot({path:path.join(output,'12-local-current-failure-original-count.png')});report.screenshots.push('12-local-current-failure-original-count.png');
      await fs.writeFile(path.join(output,'safe-local-scope-capacity.json'),JSON.stringify(cap,null,2));
    });
    await step('30_day_access_and_over_365_day_metadata_keep_original_known_and_unknown_fees',async()=>{
      const before=await inspect();await inspect({command:'advance',seconds:30*86400+1});
      for(const [method,url,data] of [['get','/v1/tasks/resilience-fallback'],['get','/v1/tasks/resilience-held/result'],['post','/v1/tasks/resilience-held/resume',{}],['post','/v1/tasks',task('resilience-held')]]){
        const row=await api(method,url,client,data);assert.equal(row.response.status(),410);assert.equal(row.body.error.code,'task_expired');
      }
      const after30=await inspect();assert.deepEqual(after30.wire_files_sha256,before.wire_files_sha256);
      assert.deepEqual(after30.receipt_artifacts_sha256,before.receipt_artifacts_sha256);
      await inspect({command:'advance',seconds:336*86400});
      const cap=await get('/monitor/capacity'),queue=await get('/monitor/queue'),after=await inspect();
      assert.equal(cap.dispatch.integrity.witness_retention_days_minimum,365);assert.equal(queue.integrity.witness_retention_days_minimum,365);
      assert.equal(after.budget['budget-primary.sqlite'].unknown_events,1);
      assert.equal(after.budget['budget-primary.sqlite'].known_usd,primaryKnown);assert.equal(after.budget['budget-disjoint.sqlite'].known_usd,disjointKnown);
      assert.deepEqual(after.receipt_artifacts_sha256,before.receipt_artifacts_sha256);assert.deepEqual(after.task_jobs_sha256,before.task_jobs_sha256);
      assert.deepEqual(after.health_sqlite_sha256,before.health_sqlite_sha256);
      assert.deepEqual(after.model_requests,[]);assert.deepEqual(after.source_requests,[]);assert.deepEqual(after.accounting_requests,[]);
      assert.equal(Object.keys(after.preservation_sha256).length,2);
      await queuePage();await page.locator('[data-queue-run="resilience-held"]').getByText('存取已到期',{exact:true}).waitFor();
      await page.locator('[data-queue-run="resilience-held"]').screenshot({path:path.join(output,'13-expired-access-retained-unknown-fees.png')});report.screenshots.push('13-expired-access-retained-unknown-fees.png');
      await fs.writeFile(path.join(output,'retained-366-day-facts.json'),JSON.stringify(after,null,2));
    });
  }
}

(process.argv.includes('--storage')?storageMain():lifecycleMain()).catch(error=>{
  process.stderr.write(error.name+': private desktop setup failed\n');process.exitCode=1;
});
