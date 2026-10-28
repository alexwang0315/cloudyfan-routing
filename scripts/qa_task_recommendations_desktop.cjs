#!/usr/bin/env node
'use strict';
// Execute only against the explicit reviewed source manifest and own fresh host.
// No route.fulfill, addInitScript, DOM injection or manufactured recommendations.
const fs=require('node:fs/promises');
const path=require('node:path');
const assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
const {execFileSync}=require('node:child_process');
const {performance}=require('node:perf_hooks');
const OUT=path.resolve(process.argv[2]||'__MISSING_OUTPUT__');
const TIMEOUTS={ui_ms:6000,ipc_ms:15000,http_seconds:5,product_poll_ms:5000,
 browser_close_ms:15000,own_pid_exit_ms:5000,supervisor:'external_release_required_no_implicit_extension'};
const report={scope:'Focused #40: real headed1280x900 + loopback, all upstreams synthetic MockTransport',
 synthetic:true,observed_schema_is_real_effect_evidence:false,timeouts:TIMEOUTS,
 started_at:new Date().toISOString(),steps:[],hosts:[],network:[],console:[],page_errors:[],blocked:[],driver_pid:process.pid};
let browser,fixture,page,currentCase='setup';
const ownedPids=new Set();
const sha=body=>createHash('sha256').update(body).digest('hex');
const fail=code=>{const error=new Error(code);error.qaCode=code;throw error;};
const check=(condition,code)=>{if(!condition)fail(code);};
const save=()=>fs.writeFile(path.join(OUT,'desktop-report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
function trackOwnPids(){
 const rows=execFileSync('/bin/ps',['-axo','pid=,ppid=,comm='],{encoding:'utf8'}).trim().split('\n').map(row=>row.trim().split(/\s+/));
 const ids=new Set([process.pid]);let changed=true;
 while(changed){changed=false;for(const row of rows)if(ids.has(Number(row[1]))&&!ids.has(Number(row[0]))){ids.add(Number(row[0]));changed=true;}}
 for(const pid of ids)if(pid!==process.pid)ownedPids.add(pid);
}
function pidGone(pid){try{process.kill(pid,0);return false;}catch(error){return error.code==='ESRCH';}}
async function ownExit(){
 const until=performance.now()+TIMEOUTS.own_pid_exit_ms;
 while([...ownedPids].some(pid=>!pidGone(pid))&&performance.now()<until)await new Promise(resolve=>setTimeout(resolve,100));
 return [...ownedPids].sort((a,b)=>a-b).map(pid=>({pid,exited:pidGone(pid)}));
}
async function bounded(promise,ms,code){let timer;try{return await Promise.race([promise,new Promise((unused,reject)=>{timer=setTimeout(()=>{const e=new Error(code);e.qaCode=code;reject(e);},ms);})]);}finally{clearTimeout(timer);}}
async function sourceManifest(){
 const pointer=process.env.CLOUDYFAN_QA_SOURCE_MANIFEST;
 check(!!pointer,'qa_explicit_frozen_source_manifest_required');
 const raw=await fs.readFile(pointer),manifest=JSON.parse(raw);
 check(manifest.schema_version==='qa40-reviewed-source-manifest-v1'&&Array.isArray(manifest.files)&&manifest.files.length>0,'qa_source_manifest_schema');
 const roots={core:await fs.realpath(process.env.CLOUDYFAN_CORE_ROOT),monitor:await fs.realpath(process.env.CLOUDYFAN_MONITOR_ROOT)};
 let count=0;const names=new Set();
 for(const row of manifest.files){
  check(['core','monitor'].includes(row.repo)&&typeof row.path==='string'&&!path.isAbsolute(row.path)&&!row.path.split(/[\\/]/).includes('..'),'qa_source_reference_path');
  check(typeof row.sha256==='string'&&/^[a-f0-9]{64}$/.test(row.sha256),'qa_source_reference_digest');
  const key=row.repo+':'+row.path;check(!names.has(key),'qa_source_reference_duplicate');names.add(key);
  const target=path.resolve(roots[row.repo],row.path);
  check(target.startsWith(roots[row.repo]+path.sep),'qa_source_reference_escape');
  const info=await fs.lstat(target);check(info.isFile()&&!info.isSymbolicLink(),'qa_source_not_regular');
  check(sha(await fs.readFile(target))===row.sha256,'qa_source_bytes_changed');count++;
 }
 return {manifest_sha256:sha(raw),verified_files:count,roots};
}
function businessState(state){const {monitor_sha256,identity_sha256,...business}=state;return business;}
async function state(command){fixture.send({command});const row=await fixture.next();check(row.type===command,'qa_state_message_type');return row.state;}
async function step(name,action){currentCase=name;console.log('START '+name);await action();trackOwnPids();report.steps.push({name,passed:true});await save();console.log('PASS '+name);}
const privateText=/PRIVATE_(?:FEATURE|RECOMMENDATION|RESILIENCE)_[A-Z_]*CANARY|Bearer|receipts-primary|budget-primary\.sqlite|future_calibration_kind|public-calibration|synthetic-calibration\.json/;
function redact(value,directory){return value.split(directory).join('[REDACTED_PRIVATE_ROOT]').replace(/PRIVATE_[A-Z_]*CANARY/g,'[REDACTED_CANARY]').replace(/Bearer\s+[^\s"<>]+/g,'[REDACTED_CREDENTIAL]').replace(/(?:receipts-primary|budget-primary\.sqlite|public-calibration|synthetic-calibration\.json)/g,'[REDACTED_PRIVATE_PATH]').replace(/future_calibration_kind/g,'[REDACTED_SOURCE_KIND]');}
async function privacy(){
 const body=await page.locator('body').innerText(),aria=await page.locator('body').ariaSnapshot();
 check(!privateText.test(body),'qa_private_dom_text_exposed');check(!privateText.test(aria),'qa_private_aria_exposed');
}
async function snap(directory,name){
 await page.screenshot({path:path.join(directory,name+'.png'),fullPage:false});
 const aria=await page.locator('body').ariaSnapshot();
 await fs.writeFile(path.join(directory,name+'.aria.txt'),redact(aria,directory),{mode:0o600});
}
async function expandTrace(run){
 const button=page.locator('tr[data-task-run="'+run+'"] [data-task-trace-toggle]');
 await button.focus();await page.keyboard.press('Enter');
 const id=await button.getAttribute('aria-controls');check(!!id,'qa_trace_controls_missing');
 const detail=page.locator('[id='+JSON.stringify(id)+']');
 await detail.getByRole('heading',{name:'任務建立時特徵',exact:true}).waitFor();return detail;
}
async function keyboardDisclosure(detail){
 const summary=detail.locator(':scope > summary');await summary.focus();await page.keyboard.press('Enter');
 check(await detail.getAttribute('open')!==null,'qa_enter_did_not_open');
 await page.keyboard.press('Space');check(await detail.getAttribute('open')===null,'qa_space_did_not_close');
 await page.keyboard.press('Space');check(await detail.getAttribute('open')!==null,'qa_space_did_not_reopen');
 return summary;
}
async function naturalPollKeepsFocus(reason,sourceDetail,hostReport){
 const summary=reason.locator(':scope > summary');await summary.focus();const id=await summary.getAttribute('id');check(!!id,'qa_reason_stable_id_missing');
 const observationStart=performance.now(),polls=[];
 // Observe two original 5s app polls. No manual fetch/refresh, clock or DOM writes.
 for(let i=0;i<2;i++){
  await page.waitForResponse(response=>new URL(response.url()).origin===hostReport.origin&&new URL(response.url()).pathname==='/monitor/snapshot'&&response.status()===200,{timeout:TIMEOUTS.ipc_ms});
  polls.push(performance.now());
  await page.waitForFunction(value=>document.activeElement?.id===value,id,{timeout:TIMEOUTS.ui_ms});
  check(await reason.getAttribute('open')!==null,'qa_poll_closed_reason');
  check(await sourceDetail.getAttribute('open')!==null,'qa_poll_closed_source');
 }
 hostReport.poll_observation={source_period_ms:5000,manually_requested:false,observed_at_ms:polls,
  elapsed_while_focused_ms:polls.at(-1)-observationStart,observed_response_gap_ms:polls[1]-polls[0],focused_summary_id:id};
}
async function verifyReferences(directory){
 const receiptRaw=await fs.readFile(path.join(directory,'fixture-receipt.json'));
 const receipt=JSON.parse(receiptRaw),names=new Set();let checked=0;
 for(const [lane,rows] of [['http',receipt.http_responses],['upstream',receipt.upstream_wire]]){
  check(Array.isArray(rows),'qa_artifact_rows_missing');
  const sequences=[];
  for(const row of rows){
   check(Number.isSafeInteger(row.sequence)&&row.sequence>0,'qa_artifact_sequence');sequences.push(row.sequence);
   if(lane==='http')check(typeof row.request_body==='string'&&typeof row.response_body==='string'&&!row.response_missing,'qa_http_body_pair_missing');
   for(const [field,hashField,sizeField] of [['request_body','request_sha256','request_bytes'],['response_body','response_sha256','response_bytes'],['body','body_sha256','body_bytes'],['headers','headers_sha256','headers_bytes']]){
    if(!Object.hasOwn(row,field))continue;
    const name=row[field];check(typeof name==='string'&&path.basename(name)===name&&!names.has(name),'qa_artifact_path_or_duplicate');names.add(name);
    const file=path.join(directory,name),info=await fs.lstat(file);check(info.isFile()&&!info.isSymbolicLink()&&info.nlink===1&&(info.mode&0o077)===0,'qa_artifact_not_private_regular');
    const bytes=await fs.readFile(file);check(bytes.length===row[sizeField]&&sha(bytes)===row[hashField],'qa_artifact_size_or_hash');
    check(bytes.length>0||field!=='response_body'||row.method==='HEAD'||[204,304].includes(row.status),'qa_artifact_unexpected_empty_response');checked++;
   }
  }
  sequences.sort((a,b)=>a-b);check(sequences.every((value,index)=>value===index+1),'qa_artifact_sequence_gap');
  check(receipt.artifact_verification?.[lane]?.status==='verified','qa_host_reference_verification_failed');
 }
 check(receipt.readonly_state_unchanged===true,'qa_host_readonly_oracle_failed');
 check(receipt.blocked_socket_attempt_count===0,'qa_host_network_guard_failed');
 check(receipt.cleanup.server_thread_exited&&receipt.cleanup.server_socket_closed&&receipt.cleanup.fixture_close_completed,'qa_host_cleanup_failed');
 return {status:'verified',checked_references:checked,unique_names:names.size,
 receipt_sha256:sha(receiptRaw),ordering:'reservation_only_not_native_http_order',synthetic:true};
}
async function scenario(kind,chromium,root,core){
 const directory=path.join(OUT,kind);const item={source_kind:kind,synthetic:true,origin:null};report.hosts.push(item);
 let context,primaryFailure;
 try{
  const {startFixture}=require(path.join(root,'scripts/qa_fixture_ipc.cjs'));
  fixture=startFixture(path.join(core,'.venv/bin/python'),[path.join(__dirname,'qa_task_recommendations_http.py'),'--output',directory,'--source-kind',kind],
   {cwd:root,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[path.join(root,'src'),path.join(core,'src')].join(path.delimiter)},responseTimeoutMs:TIMEOUTS.ipc_ms});
  const ready=await fixture.next();check(ready.type==='ready'&&ready.synthetic===true,'qa_fixture_ready_missing');
  check(ready.observed_schema_is_actual_observation===false,'qa_synthetic_boundary_missing');
  item.fixture_pid=ready.fixture_pid;ownedPids.add(ready.fixture_pid);item.origin=ready.origin;item.ready_state=ready.state;
  context=await browser.newContext({viewport:{width:1280,height:900},locale:'zh-TW',serviceWorkers:'block'});
  await context.route('**/*',async route=>{
   const request=route.request();let same=false;try{same=new URL(request.url()).origin===ready.origin;}catch{}
   if(same&&request.method()==='GET')await route.continue();
   else{report.blocked.push({code:same?'browser_non_read_request':'browser_external_request',method:request.method()});await route.abort();}
  });
  page=await context.newPage();page.setDefaultTimeout(TIMEOUTS.ui_ms);trackOwnPids();
  page.on('pageerror',()=>report.page_errors.push({code:'page_error',host:kind}));
  page.on('console',message=>{if(['warning','error'].includes(message.type()))report.console.push({level:message.type(),args_count:message.args().length,host:kind});});
  page.on('response',response=>{const url=new URL(response.url());report.network.push({host:kind,method:response.request().method(),
   path_code:url.pathname==='/monitor/snapshot'?'snapshot':url.pathname==='/monitor/pools'?'pools':'other_same_origin_read',status:response.status(),at_ms:performance.now()});});
  await page.goto(ready.origin,{waitUntil:'domcontentloaded'});
  await page.getByRole('heading',{name:'路由運作，一眼掌握。'}).waitFor();
  check((await page.locator('#environment').innerText()).includes('模擬上游'),'qa_environment_not_synthetic');
  const baseline=await state('baseline');item.read_baseline=baseline;
  assert.deepEqual(businessState(baseline),businessState(ready.state));
  await page.getByRole('link',{name:'↳ 任務與升級'}).click();
  for(const entry of ready.cases){
   await step(kind+'_'+entry.run,async()=>{
    const detail=await expandTrace(entry.run),advice=detail.locator('[data-routing-recommendation]');
    await advice.getByRole('heading',{name:'建立時路由建議',exact:true}).waitFor();
    const text=await advice.innerText();check(text.includes('未採用')||['legacy','unavailable'].includes(entry.expected),'qa_unadopted_label_missing');
    check(await advice.locator('button,input,select,textarea,[role="button"]').count()===0,'qa_advice_has_route_control');
    if(entry.expected==='legacy'){
     const reason=advice.locator('[data-recommendation-reasons]').first();await keyboardDisclosure(reason);
     check(text.includes('未提供')&&(await reason.innerText()).includes('creation_recommendation_not_provided'),'qa_legacy_not_visible');
    }
    if(entry.expected==='unavailable'){
     const reason=advice.locator('[data-recommendation-reasons]').first();await keyboardDisclosure(reason);
     const reasonText=await reason.innerText();
     check(text.includes('無法取得')&&reasonText.includes('creation_recommendation_evidence_unavailable'),'qa_capture_off_unavailable_not_visible');
     check(!reasonText.includes('creation_recommendation_not_provided'),'qa_capture_off_mislabelled_legacy');
    }
    if(entry.expected==='unknown'){
     check(text.includes('未知'),'qa_unknown_not_visible');
     check(await advice.locator('[data-recommendation-kind="observe_original_route"]').count()===1,'qa_observe_advice_missing');
    }
    if(entry.expected==='not_applicable'){
     const reason=advice.locator('[data-recommendation-reasons]').first();await keyboardDisclosure(reason);
     check(text.includes('不適用')&&(await reason.innerText()).includes('original_mode_has_no_classification'),'qa_na_not_visible');
    }
    if(entry.expected==='unsupported'){
     const reason=advice.locator('[data-recommendation-reasons]').first();await keyboardDisclosure(reason);
     const reasonText=await reason.innerText();
     check(text.includes('未支援')&&reasonText.includes('exact_calibration_source_schema_unsupported'),'qa_unsupported_safe_code_not_visible');
     check(!reasonText.includes('ui_recommendation_reason_unrecognized'),'qa_unsupported_reason_not_recognized');
     check(await advice.locator('[data-recommendation-comparison]').count()===0,'qa_unsupported_comparison_visible');
    }
    if(entry.expected==='candidate_expired'){
     const reason=advice.locator('[data-recommendation-reasons]').first();await keyboardDisclosure(reason);
     const reasonText=await reason.innerText();
     check(text.includes('未知')&&reasonText.includes('候選證據')&&reasonText.includes('candidate_evidence_expired_at_acceptance'),'qa_candidate_expiry_reason_not_visible');
     check(!reasonText.includes('exact_classifier_calibration_expired_at_acceptance'),'qa_candidate_expiry_mislabelled_classifier');
     check(await advice.locator('[data-recommendation-comparison]').count()===0,'qa_expired_candidate_comparison_visible');
    }
    if(entry.expected==='exact'){
     const compare=advice.locator('[data-recommendation-kind="compare_skipping_classification"]');
     const body=await compare.innerText();
     check(body.includes('proven_insufficient')&&body.includes('-1.919')&&body.includes('USD'),'qa_exact_cost_not_visible');
     check(body.includes('條件範圍數')&&/\b3\b/.test(body),'qa_exact_scope_not_visible');
     check(body.includes(kind),'qa_exact_source_code_missing');
     check(body.includes(kind==='offline_fixture'?'離線合成校準':'來源種類標記不證明真實成效'),'qa_source_boundary_not_visible');
     const capability=advice.locator('[data-recommendation-kind="need_capability_context_evidence"]');
     const reason=capability.locator('[data-recommendation-reasons]');await keyboardDisclosure(reason);
     const reasonText=await reason.innerText();
     check(reasonText.includes('exact_input_tokens_unknown')&&reasonText.includes('declared_context_is_not_measured_usage'),'qa_context_reasons_not_visible');
     const source=compare.locator('[data-recommendation-comparison] details[data-observation-detail]');await keyboardDisclosure(source);
     await naturalPollKeepsFocus(reason,source,item);
    }
    if(entry.run==='qa40-features-complete'||entry.run==='qa40-unknown'){
     const features=detail.locator('[data-task-features]');
     const outer=features.locator('details[data-observation-detail]').first();await keyboardDisclosure(outer);
     const featureText=await features.innerText();
     check(featureText.includes('local-deterministic-v2')&&featureText.includes('UTF-8 bytes')&&featureText.includes('Unicode codepoints'),'qa_original39_features_missing');
    }
    if(entry.run==='qa40-features-legacy')check(/無法確認/.test(await detail.locator('[data-task-features]').innerText()),'qa_original39_capture_off_unavailable_changed');
    await privacy();await advice.scrollIntoViewIfNeeded();await snap(directory,entry.run);
   });
  }
  const final=await state('state');assert.deepEqual(final,baseline);item.readonly_state_unchanged=true;
 }catch(error){
  primaryFailure=error;item.failure={step:currentCase,code:error.qaCode||'qa_desktop_or_setup_failed'};
  if(page)await snap(directory,'desktop-failure').catch(()=>{item.failure_capture_failed=true;});
  throw error;
 }finally{
  const cleanupFailures=[];
  if(context){try{await context.close();}catch{cleanupFailures.push('qa_context_close_failed');}}page=null;
  if(fixture){try{item.fixture_exit=await fixture.stop();
   if(item.fixture_exit.code!==0||item.fixture_exit.signal||item.fixture_exit.timed_out||item.fixture_exit.cleanup_failed||item.fixture_exit.stderr_present)cleanupFailures.push('qa_fixture_exit_failed');
  }catch{cleanupFailures.push('qa_fixture_stop_failed');}finally{fixture=null;}}
  try{item.references=await verifyReferences(directory);}catch{cleanupFailures.push('qa_reference_verification_failed');}
  item.cleanup_failures=cleanupFailures;
  if(cleanupFailures.length){report.passed=false;process.exitCode=1;if(!primaryFailure)fail(cleanupFailures[0]);}
 }
}
async function main(){
 process.umask(0o077);check(process.argv[2]&&process.env.CLOUDYFAN_MONITOR_ROOT&&process.env.CLOUDYFAN_CORE_ROOT,'qa_explicit_paths_required');
 await fs.mkdir(OUT,{recursive:false,mode:0o700});await save();
 report.source_before=await sourceManifest();
 const root=report.source_before.roots.monitor,core=report.source_before.roots.core;
 report.driver_sha256=sha(await fs.readFile(__filename));report.host_sha256=sha(await fs.readFile(path.join(__dirname,'qa_task_recommendations_http.py')));
 const {chromium}=require(process.env.CLOUDYFAN_PLAYWRIGHT_MODULE||process.env.PLAYWRIGHT_PATH||'/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
 browser=await chromium.launch({channel:'chrome',headless:false});report.browser=browser.version();trackOwnPids();
 for(const kind of ['offline_fixture','observed_calibration'])await scenario(kind,chromium,root,core);
 report.source_after=await sourceManifest();assert.deepEqual(report.source_after,report.source_before);
 report.driver_sha256_after=sha(await fs.readFile(__filename));
 report.host_sha256_after=sha(await fs.readFile(path.join(__dirname,'qa_task_recommendations_http.py')));
 check(report.driver_sha256_after===report.driver_sha256&&report.host_sha256_after===report.host_sha256,'qa_executed_code_changed');
 report.passed=true;
}
main().catch(async error=>{
 report.passed=false;report.failure={step:currentCase,code:error.qaCode||'qa_desktop_or_setup_failed'};process.exitCode=1;
 if(page){const directory=path.join(OUT,report.hosts.at(-1)?.source_kind||'offline_fixture');await snap(directory,'desktop-failure').catch(()=>{});}
}).finally(async()=>{
 try{if(browser)await bounded(browser.close(),TIMEOUTS.browser_close_ms,'qa_browser_close_timeout');}
 catch{report.passed=false;report.browser_cleanup_failed=true;process.exitCode=1;}
 if(fixture){const result=await fixture.stop();report.emergency_fixture_exit=result;fixture=null;report.passed=false;process.exitCode=1;}
 report.own_pids=await ownExit();
 if(report.own_pids.some(row=>!row.exited)||report.blocked.length||report.console.length||report.page_errors.length){report.passed=false;process.exitCode=1;}
 report.finished_at=new Date().toISOString();await save().catch(()=>{process.exitCode=1;});
 console.log(JSON.stringify({passed:report.passed===true,report:path.join(OUT,'desktop-report.json')}));
});
