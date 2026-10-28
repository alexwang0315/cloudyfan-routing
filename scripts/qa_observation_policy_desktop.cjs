#!/usr/bin/env node
'use strict';
// Real own headed Chrome and loopback. No generated JSON or DOM writes.
const fs=require('node:fs/promises');
const path=require('node:path');
const {createHash}=require('node:crypto');
const {execFileSync}=require('node:child_process');
const {performance}=require('node:perf_hooks');
const {startFixture}=require('./qa_fixture_ipc.cjs');
const ROOT=path.resolve(__dirname,'..'),CORE=process.env.CLOUDYFAN_CORE_ROOT;
const OUT=path.resolve(process.argv[2]||'__MISSING_OUTPUT__');
const phase=process.argv[3],owned=new Set();
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const report={schema_version:'qa41-observation-policy-desktop-v1',phase,
 scope:'Focused #41; actual headed Chrome1280x900 + real loopback; all upstreams MockTransport',
 started_at:new Date().toISOString(),driver_pid:process.pid,steps:[],requests:[],network:[],console:[],page_errors:[],blocked:[]};
let fixture,browser,page,currentStep='setup';
function check(value,code){if(!value){const error=new Error(code);error.qaCode=code;throw error;}}
async function save(){await fs.writeFile(path.join(OUT,'desktop-report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});}
function track(){
 const rows=execFileSync('/bin/ps',['-axo','pid=,ppid=,comm='],{encoding:'utf8'}).trim().split('\n').map(line=>line.trim().split(/\s+/));
 const ids=new Set([process.pid]);let changed=true;
 while(changed){changed=false;for(const row of rows)if(ids.has(Number(row[1]))&&!ids.has(Number(row[0]))){ids.add(Number(row[0]));changed=true;}}
 for(const pid of ids)if(pid!==process.pid)owned.add(pid);
}
function gone(pid){try{process.kill(pid,0);return false;}catch(error){return error.code==='ESRCH';}}
async function sourceManifest(){
 const rows=[];
 async function walk(root,repo,relative){for(const item of await fs.readdir(path.join(root,relative),{withFileTypes:true})){
  if(item.name==='__pycache__')continue;const file=path.join(relative,item.name);
  if(item.isDirectory())await walk(root,repo,file);else if(item.isFile())rows.push({repo,path:file,sha256:sha(await fs.readFile(path.join(root,file)))});
 }}
 for(const [repo,root] of [['core',CORE],['monitor',ROOT]]){
  for(const dir of ['src','tests','scripts','config','.githooks','.github']){
   try{await walk(root,repo,dir);}catch(error){if(error.code!=='ENOENT')throw error;}
  }
  for(const file of ['pyproject.toml','uv.lock']){
   try{rows.push({repo,path:file,sha256:sha(await fs.readFile(path.join(root,file)))});}catch(error){if(error.code!=='ENOENT')throw error;}
  }
 }
 return rows.sort((a,b)=>(a.repo+':'+a.path).localeCompare(b.repo+':'+b.path));
}
async function frozenManifest(){
 check(process.env.CLOUDYFAN_QA_FROZEN_RELEASE==='issue41-desktop-v1'&&!!process.env.CLOUDYFAN_QA_SOURCE_MANIFEST,'qa_explicit_frozen_release_required');
 const raw=await fs.readFile(process.env.CLOUDYFAN_QA_SOURCE_MANIFEST),manifest=JSON.parse(raw);
 check(manifest.schema_version==='qa41-reviewed-source-manifest-v1'&&Array.isArray(manifest.files),'qa_frozen_manifest_schema');
 check(JSON.stringify(manifest.files)===JSON.stringify(report.source_before),'qa_frozen_manifest_bytes_or_scope_differ');
 return {sha256:sha(raw),verified_files:manifest.files.length};
}
async function snap(name){
 await page.screenshot({path:path.join(OUT,name+'.png'),fullPage:false});
 await fs.writeFile(path.join(OUT,name+'.aria.txt'),await page.locator('body').ariaSnapshot(),{mode:0o600});
}
async function verifyReferences(receipt){
 const names=new Set();let count=0;
 for(const rows of [receipt.http_responses,receipt.upstream_wire]){
  check(Array.isArray(rows),'qa_evidence_rows_missing');
  const sequences=[];
  for(const row of rows){
   check(Number.isSafeInteger(row.sequence)&&row.sequence>0,'qa_evidence_sequence_invalid');sequences.push(row.sequence);
   if(Object.hasOwn(row,'status'))check(typeof row.request_body==='string'&&typeof row.response_body==='string','qa_http_pair_missing');
   for(const [field,hashKey,sizeKey] of [['request_body','request_sha256','request_bytes'],['response_body','response_sha256','response_bytes'],['body','body_sha256','body_bytes'],['headers','headers_sha256','headers_bytes']]){
    if(!Object.hasOwn(row,field))continue;
    const filename=row[field];check(typeof filename==='string'&&path.basename(filename)===filename&&!names.has(filename),'qa_evidence_reference_duplicate_or_escape');names.add(filename);
    const file=path.join(OUT,'host',filename),info=await fs.lstat(file);
    check(info.isFile()&&!info.isSymbolicLink()&&info.nlink===1&&(info.mode&0o077)===0,'qa_evidence_not_private_regular');
    const bytes=await fs.readFile(file);
    check(bytes.length===row[sizeKey]&&sha(bytes)===row[hashKey],'qa_evidence_bytes_or_sha_changed');
    check(field!=='response_body'||bytes.length>0||row.method==='HEAD'||[204,304].includes(row.status),'qa_http_response_unexpected_empty');count++;
   }
  }
  sequences.sort((a,b)=>a-b);check(sequences.every((value,index)=>value===index+1),'qa_evidence_sequence_gap');
 }
 if(phase==='desktop-frozen'){
  const fault=receipt.capture_failure;
  check(fault?.original_accepted_status===202&&fault.replay_status===202
   &&JSON.stringify(fault.repeated_get_statuses)==='[202,202]'&&fault.seal_attempts===1
   &&fault.independent_seal_still_missing&&fault.record_and_anchor_unchanged
   &&fault.readonly_business_state_unchanged,'qa_capture_failure_oracle_missing');
  for(const field of ['record','anchor','fault_receipt']){
   const row=fault.preserved?.[field],filename=row?.reference;
   check(typeof filename==='string'&&path.basename(filename)===filename&&!names.has(filename),'qa_capture_failure_reference_invalid');names.add(filename);
   const file=path.join(OUT,'host',filename),info=await fs.lstat(file),bytes=await fs.readFile(file);
   check(info.isFile()&&!info.isSymbolicLink()&&info.nlink===1&&(info.mode&0o077)===0
    &&bytes.length===row.bytes&&sha(bytes)===row.sha256,'qa_capture_failure_reference_changed');count++;
  }
 }
 return {status:'verified',checked_references:count,unique_names:names.size,ordering:'recorder_reservation_only_not_native_http_order'};
}
async function step(name,action){currentStep=name;console.log('START '+name);await action();report.steps.push({name,passed:true});await save();console.log('PASS '+name);}
async function command(name,parameters={}){fixture.send({command:name,...parameters});const value=await fixture.next();check(value.type===name,'qa_host_command_type');return value;}
const privatePattern=/PRIVATE_(?:OBSERVATION|FEATURE|RECOMMENDATION|RESILIENCE)_[A-Z_]*CANARY|Bearer|receipts-primary|budget-primary\.sqlite/;
async function privacy(){
 check(!privatePattern.test(await page.locator('body').innerText()),'qa_private_dom_exposed');
 check(!privatePattern.test(await page.locator('body').ariaSnapshot()),'qa_private_aria_exposed');
 check(!privatePattern.test(JSON.stringify(report.console)),'qa_private_console_exposed');
}
async function settle(){await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));}
const policyActive=()=>page.locator('#observation-policy-active');
async function expectPolicy(version){
 const value=version===null?'operator':version;
 await page.waitForFunction(expected=>document.getElementById('observation-policy-active')?.dataset.observationPolicyActive===expected,value,{timeout:6000});
}
async function selectPolicy(version,{keyboard='Enter'}={}){
 if(version===null)await page.locator('#observation-policy-disable').focus();
 else{await page.locator('#observation-policy-version').selectOption(version);await page.locator('#observation-policy-activate').focus();}
 await page.keyboard.press(keyboard);await page.locator('#confirm').waitFor({state:'visible'});
 check((await page.locator('#confirm-description').innerText()).includes('既有任務'),'qa_policy_confirmation_scope_missing');
}
async function confirmPolicy(version){
 const response=page.waitForResponse(value=>new URL(value.url()).pathname==='/monitor/observation-policy/activate'&&value.request().method()==='POST');
 await page.locator('#apply-confirm').click();const actual=await response;
 check(actual.status()===200,'qa_operator_activation_http_failed');
 const body=actual.request().postDataJSON();
 check(Object.keys(body).sort().join(',')==='revision,version'&&body.version===version&&Number.isSafeInteger(body.revision),'qa_activation_payload_differs');
 await page.locator('#confirm').waitFor({state:'hidden'});await expectPolicy(version);
}
async function trace(run){
 const row=page.locator('tr[data-task-run="'+run+'"]'),button=row.locator('[data-task-trace-toggle]');
 await button.waitFor({timeout:15000});
 if(await button.getAttribute('aria-expanded')!=='true'){await button.focus();await page.keyboard.press('Enter');}
 const id=await button.getAttribute('aria-controls');check(!!id,'qa_trace_id_missing');
 const detail=page.locator('[id='+JSON.stringify(id)+']');await detail.getByRole('heading',{name:'任務建立時特徵',exact:true}).waitFor();return detail;
}
async function disclosure(detail){
 const summary=detail.locator(':scope > summary');await summary.focus();
 if(await detail.getAttribute('open')===null)await page.keyboard.press('Enter');
 check(await detail.getAttribute('open')!==null,'qa_enter_disclosure_did_not_open');
 await page.keyboard.press('Space');check(await detail.getAttribute('open')===null,'qa_space_disclosure_did_not_close');
 await page.keyboard.press('Space');check(await detail.getAttribute('open')!==null,'qa_space_disclosure_did_not_reopen');return summary;
}
async function desktop(ready){
 await step('policy_read_is_independent_of_held_history_and_long_task',async()=>{
  check(ready.long_task_held===true&&ready.state.mock_hold_started,'qa_real_held_model_required');
  await page.getByRole('heading',{name:'建立時觀測規則',exact:true}).waitFor();
  await page.getByText('尚未由操作員採用；沿用主機既有設定',{exact:true}).waitFor();
  await command('await-history-hold');
  check(report.network.some(row=>row.path==='/monitor/observation-policy'&&row.status===200),'qa_direct_policy_read_missing');
  check(!report.network.some(row=>row.path==='/monitor/snapshot'&&row.status===200),'qa_history_not_still_held');
  check((await page.locator('.observation-policy-facts').innerText()).includes('已啟用'),'qa_host_capture_mislabelled_disabled');
  await privacy();await snap('policy-host-configuration');
 });
 await step('operator_deliberately_enables_registered_advice',async()=>{
  const before=report.network.filter(row=>row.method==='POST').length;
  await selectPolicy('features-v2-advice-v1');await page.locator('#cancel-confirm').click();
  check(report.network.filter(row=>row.method==='POST').length===before,'qa_cancel_applied_policy');
  await selectPolicy('features-v2-advice-v1');await confirmPolicy('features-v2-advice-v1');
  check((await policyActive().innerText()).includes('觀察性建議 v1'),'qa_advice_policy_not_visible');
  await command('new-task',{case:'advice'});await command('new-task',{case:'capture-failure'});
  await privacy();await snap('policy-advice-enabled');
 });
 await step('operator_disables_only_new_capture_and_rolls_back',async()=>{
  await selectPolicy(null,{keyboard:'Space'});await confirmPolicy(null);await command('new-task',{case:'disabled'});
  await privacy();await snap('policy-new-capture-disabled');
  await selectPolicy('features-v1');await confirmPolicy('features-v1');await command('new-task',{case:'rollback'});
  await privacy();await snap('policy-v1-rollback');
  check((await command('state')).state.model_calls===ready.state.model_calls,'qa_policy_change_created_upstream_call');
 });
 await step('saved_old_new_missing_and_failed_observations_remain_distinct',async()=>{
  await command('release-history');await page.locator('nav a[href="#tasks"]').click();
  for(const [run,version,state] of [
   ['qa41-v1-complete','local-deterministic-v1','available'],['qa41-v1-running','local-deterministic-v1','running'],
   ['qa41-host-capture-gap',null,'unavailable'],['qa41-advice-new','local-deterministic-v2','advice'],
   ['qa41-capture-failure',null,'unavailable'],['qa41-disabled',null,'missing'],['qa41-v1-rollback','local-deterministic-v1','available']]){
   const detail=await trace(run),features=detail.locator('[data-task-features]'),advice=detail.locator('[data-routing-recommendation]');
   const text=await features.innerText();
   if(version)check(text.includes(version),'qa_saved_feature_version_missing');
   if(version==='local-deterministic-v1')check(text.includes('其他欄位未提供'),'qa_v1_missing_new_fields_not_explicit');
   if(state==='running')check((await page.locator('tr[data-task-run="'+run+'"]').innerText()).includes('執行中'),'qa_original_task_no_longer_held');
   if(state==='missing')check(text.includes('未提供')&&!text.includes('當時特徵證據無法確認'),'qa_missing_features_mislabelled');
   if(state==='unavailable')check(text.includes('無法確認')&&(await advice.innerText()).includes('無法取得'),'qa_failed_evidence_not_visible');
   if(state==='advice')check((await advice.innerText()).includes('未知')&&(await advice.innerText()).includes('未採用'),'qa_unknown_unadopted_advice_missing');
   check(await advice.locator('button,input,select,textarea,[role="button"]').count()===0,'qa_saved_advice_has_route_controls');
   await features.scrollIntoViewIfNeeded();await privacy();await snap(run);
  }
 });
 await step('two_natural_five_second_polls_preserve_multiple_disclosures_and_focus',async()=>{
  await command('read-baseline');
  const detail=await trace('qa41-advice-new'),features=detail.locator('[data-task-features]');
  const outer=features.locator('details[data-observation-detail]').first();await disclosure(outer);
  const reason=features.locator('[data-feature-reasons="input_tokens"]');await disclosure(reason);
  const second=features.locator('[data-feature-reasons="context_requirements"]');await disclosure(second);
  const adviceReason=detail.locator('[data-recommendation-kind="need_capability_context_evidence"] [data-recommendation-reasons]');await disclosure(adviceReason);
  const summary=reason.locator(':scope > summary');await summary.focus();const id=await summary.getAttribute('id');
  check(!!id,'qa_stable_feature_summary_id_missing');const started=performance.now(),polls=[];
  for(let i=0;i<2;i++){
   await page.waitForResponse(response=>new URL(response.url()).pathname==='/monitor/snapshot'&&response.status()===200,{timeout:15000});polls.push(performance.now());
   await page.waitForFunction(value=>document.activeElement?.id===value,id,{timeout:6000});
   for(const open of [outer,reason,second,adviceReason])check(await open.getAttribute('open')!==null,'qa_poll_closed_saved_disclosure');
  }
  check(polls[1]-polls[0]>=4000,'qa_natural_poll_interval_not_observed');
  report.natural_poll={source_period_ms:5000,manually_requested:false,observed_ms:polls,elapsed_ms:polls[1]-started,response_gap_ms:polls[1]-polls[0],focused_summary_id:id};
  await command('verify-readonly');await privacy();await snap('natural-polls-focus-preserved');
 });
 await step('disappearing_saved_details_return_focus_to_task_search',async()=>{
  await command('corrupt-evidence');
  await page.waitForResponse(response=>new URL(response.url()).pathname==='/monitor/snapshot'&&response.status()===200,{timeout:15000});
  await page.waitForFunction(()=>document.activeElement?.id==='task-search',null,{timeout:6000});
  const detail=await trace('qa41-advice-new');
  check((await detail.locator('[data-task-features]').innerText()).includes('無法確認'),'qa_corrupt_feature_details_not_removed');
  check(await detail.locator('[data-feature-reasons]').count()===0,'qa_old_feature_reason_survived_corruption');
  await privacy();await snap('missing-details-focus-fallback');
 });
 await step('stale_operator_revision_is_rejected_with_safe_recovery',async()=>{
  await page.locator('nav a[href="#observations"]').click();await expectPolicy('features-v1');
  await selectPolicy('features-v2');await command('external-change',{version:'features-v2-advice-v1'});
  const response=page.waitForResponse(value=>new URL(value.url()).pathname==='/monitor/observation-policy/activate'&&value.request().method()==='POST');
  await page.locator('#apply-confirm').click();check((await response).status()===409,'qa_stale_revision_not_rejected');
  await page.getByText('設定已由另一位操作員更新。本次未套用，請重新讀取後再確認。',{exact:true}).first().waitFor();
  await page.locator('#reload').click();await expectPolicy('features-v2-advice-v1');await privacy();await snap('stale-revision-recovered');
 });
 await step('late_policy_response_cannot_replace_a_newer_read',async()=>{
  await command('hold-policy');await page.locator('#reload').click();await command('await-policy-hold');
  await command('external-change',{version:'features-v2'});await page.locator('#reload').click();await expectPolicy('features-v2');
  const late=page.waitForResponse(response=>new URL(response.url()).pathname==='/monitor/observation-policy'&&response.status()===200);
  await command('release-policy');await (await late).finished();await settle();
  check(await policyActive().getAttribute('data-observation-policy-active')==='features-v2','qa_late_read_replaced_current_policy');
  await privacy();await snap('late-policy-read-fenced');
 });
 await step('logout_and_changed_operator_fence_old_policy_response',async()=>{
  await command('hold-policy');await page.locator('#reload').click();await command('await-policy-hold');
  await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});
  check(await page.locator('#observation-policy-page').count()===0,'qa_logout_kept_policy_data');
  await command('rotate-operator');await command('external-change',{version:null});
  // Read the real demo bootstrap credential in memory only; never serialize it.
  const bootstrap=await page.request.get(ready.origin+'/demo/session');check(bootstrap.status()===200,'qa_demo_bootstrap_failed');
  const session=await bootstrap.json();await page.locator('#token').fill(session.token);await page.locator('#login-form button[type="submit"]').click();
  await page.locator('#login').waitFor({state:'hidden'});await expectPolicy(null);
  const late=page.waitForResponse(response=>new URL(response.url()).pathname==='/monitor/observation-policy'&&response.status()===200);
  await command('release-policy');await (await late).finished();await settle();
  check((await policyActive().innerText()).includes('停用新任務擷取與建議'),'qa_old_session_restored_policy');
  check(await page.locator('#login').isVisible()===false,'qa_old_session_reopened_login');
  await privacy();await snap('operator-session-response-fenced');
 });
 await step('original_held_task_finishes_under_its_saved_version',async()=>{
  await command('release-model');const final=(await command('state')).state;
  check(final.model_calls===ready.state.model_calls&&final.source_calls===0&&final.accounting_calls===0,'qa_observation_added_upstream_work');
  report.final_model_calls=final.model_calls;
  check(report.blocked.length===0&&report.page_errors.length===0,'qa_browser_runtime_or_network_error');
  check(!report.console.some(row=>['warning','error'].includes(row.level)),'qa_browser_console_warning_or_error');
 });
}
async function main(){
 process.umask(0o077);check(process.argv[2]&&CORE&&process.env.CLOUDYFAN_QA_PYTHON,'qa_explicit_paths_required');
 check(['old-ui-red','desktop-frozen'].includes(phase),'qa_phase_not_released');
 await fs.mkdir(OUT,{recursive:false,mode:0o700});await save();
 report.source_before=await sourceManifest();await save();
 if(phase==='desktop-frozen')report.frozen_manifest=await frozenManifest();
 fixture=startFixture(process.env.CLOUDYFAN_QA_PYTHON,[path.join(ROOT,'scripts/qa_observation_policy_http.py'),
  '--output',path.join(OUT,'host'),'--phase',phase],{cwd:ROOT,
  env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',UV_OFFLINE:'1',PYTHONPATH:[path.join(ROOT,'src'),path.join(CORE,'src')].join(path.delimiter)},responseTimeoutMs:15000});
 const ready=await fixture.next();check(ready.type==='ready'&&ready.synthetic===true,'qa_real_host_ready_missing');
 report.fixture_pid=ready.fixture_pid;owned.add(ready.fixture_pid);report.origin=ready.origin;
 const {chromium}=require(process.env.CLOUDYFAN_PLAYWRIGHT_MODULE||'/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
 browser=await chromium.launch({channel:'chrome',headless:false});report.browser=browser.version();track();
 const context=await browser.newContext({viewport:{width:1280,height:900},locale:'zh-TW',serviceWorkers:'block'});
 await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin===ready.origin&&(request.method()==='GET'||phase==='desktop-frozen'&&request.method()==='POST'&&url.pathname==='/monitor/observation-policy/activate'))await route.continue();
  else{report.blocked.push({method:request.method(),same_origin:new URL(request.url()).origin===ready.origin});await route.abort();}
 });
 page=await context.newPage();page.setDefaultTimeout(6000);track();
 page.on('console',message=>report.console.push({level:message.type(),text:message.text()}));
 page.on('pageerror',error=>report.page_errors.push({name:error.name,message:error.message}));
 page.on('request',request=>report.requests.push({method:request.method(),path:new URL(request.url()).pathname,at_ms:performance.now()}));
 page.on('response',response=>report.network.push({method:response.request().method(),path:new URL(response.url()).pathname,status:response.status(),at_ms:performance.now()}));
 await page.goto(ready.origin+(phase==='desktop-frozen'?'/#observations':''),{waitUntil:'domcontentloaded'});
 if(phase==='desktop-frozen'){await desktop(ready);report.passed=true;return;}
 await page.getByRole('heading',{name:'路由運作，一眼掌握。'}).waitFor();
 await snap('old-ui-before-policy-navigation');
 check(!/PRIVATE_OBSERVATION_POLICY_CANARY|Bearer/.test(await page.locator('body').innerText()),'qa_private_dom_exposed');
 check(!/PRIVATE_OBSERVATION_POLICY_CANARY|Bearer/.test(await page.locator('body').ariaSnapshot()),'qa_private_aria_exposed');
 report.observed={real_operator_session:report.network.some(row=>row.path==='/monitor/session'&&row.status===200),
  policy_navigation_count:await page.locator('nav a[href="#observations"]').count()};
 check(report.observed.real_operator_session,'qa_operator_session_missing');
 check(report.observed.policy_navigation_count===1,'qa_observation_policy_control_missing');
 report.passed=true;
}
main().catch(async error=>{
 report.passed=false;report.failure={step:currentStep,code:error.qaCode||'qa_desktop_failed'};process.exitCode=1;
 const original=String(error.stack||error);
 await fs.writeFile(path.join(OUT,'desktop-exception.txt'),original+'\n',{mode:0o600,flag:'wx'});
 report.failure.original_private_reference={path:'desktop-exception.txt',sha256:sha(original+'\n'),bytes:Buffer.byteLength(original+'\n')};
 if(page)await snap('desktop-failure').catch(()=>{report.failure_capture_failed=true;});
}).finally(async()=>{
 track();if(browser)try{await browser.close();}catch{report.browser_close_failed=true;process.exitCode=1;}
 if(fixture){report.fixture_exit=await fixture.stop();if(report.fixture_exit.code!==0||report.fixture_exit.signal||report.fixture_exit.timed_out||report.fixture_exit.stderr_present){report.cleanup_failed=true;process.exitCode=1;}}
 try{
  const receipt=JSON.parse(await fs.readFile(path.join(OUT,'host','fixture-receipt.json')));
  report.host_verification={artifacts:receipt.artifact_verification,upstream:receipt.upstream_artifact_verification,
   business_state_unchanged:receipt.business_state_unchanged,readonly_oracle_verified:receipt.readonly_oracle_verified,
   held_model_completed:receipt.held_model_completed,capture_failure:receipt.capture_failure,
   blocked_socket_attempt_count:receipt.blocked_socket_attempt_count,cleanup:receipt.cleanup};
  report.independent_reference_verification=await verifyReferences(receipt);
  check(receipt.artifact_verification.status==='verified'&&receipt.upstream_artifact_verification.status==='verified'
   &&(phase==='old-ui-red'?receipt.business_state_unchanged:receipt.readonly_oracle_verified&&receipt.held_model_completed)
   &&receipt.blocked_socket_attempt_count===0&&Object.values(receipt.cleanup).every(Boolean),'qa_host_cleanup_or_evidence_failed');
 }catch{report.host_verification_failed=true;process.exitCode=1;}
 const end=performance.now()+5000;while([...owned].some(pid=>!gone(pid))&&performance.now()<end)await new Promise(resolve=>setTimeout(resolve,100));
 report.owned_child_pids_exited=[...owned].map(pid=>({pid,exited:gone(pid)}));
 if(report.owned_child_pids_exited.some(row=>!row.exited)){report.cleanup_failed=true;process.exitCode=1;}
 report.source_after=await sourceManifest();report.source_unchanged=JSON.stringify(report.source_before)===JSON.stringify(report.source_after);
 if(!report.source_unchanged){report.source_changed=true;process.exitCode=1;}
 report.finished_at=new Date().toISOString();await save();
 console.log(JSON.stringify({passed:report.passed,failure:report.failure,report:path.join(OUT,'desktop-report.json')}));
});
