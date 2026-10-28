#!/usr/bin/env node
'use strict';
// Frozen main-service HTTP + actual headed Chrome. No report fixtures or DOM injection.
const fs=require('node:fs/promises');
const path=require('node:path');
const {createHash}=require('node:crypto');
const {execFileSync,spawn}=require('node:child_process');
const readline=require('node:readline');
const {performance}=require('node:perf_hooks');
const ROOT=path.resolve(__dirname,'..'),CORE=process.env.CLOUDYFAN_CORE_ROOT;
const OUT=path.resolve(process.argv[2]||'__MISSING_OUTPUT__'),phase=process.argv[3],owned=new Set();
const sha=body=>createHash('sha256').update(body).digest('hex');
const report={schema_version:'qa42-saved-comparisons-desktop-v1',phase,synthetic:true,
 scope:'Actual headed Chrome1280x900 and real main-service HTTP/Core/Budget/Store/Identity/Queue; upstreams MockTransport only',
 started_at:new Date().toISOString(),driver_pid:process.pid,steps:[],requests:[],network:[],console:[],page_errors:[],blocked:[]};
let fixture,browser,page,currentStep='setup';
function check(value,code){if(!value){const error=Error(code);error.qaCode=code;throw error;}}
async function save(){await fs.writeFile(path.join(OUT,'desktop-report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});}
function git(root,args){return execFileSync('/usr/bin/git',['--no-optional-locks','-C',root,...args],{encoding:'utf8',env:{...process.env,GIT_OPTIONAL_LOCKS:'0'}});}
async function sourceManifest(){
 const repositories=[],files=[];
 for(const [repo,root] of [['core',CORE],['monitor',ROOT]]){
  const head=git(root,['rev-parse','HEAD']).trim();check(git(root,['status','--porcelain=v1'])==='','qa_clean_committed_source_required');
  repositories.push({repo,head});
  for(const row of git(root,['ls-tree','-r','-z','--full-tree','HEAD']).split('\0').filter(Boolean)){
   const [header,relative]=row.split('\t'),[mode,type,blob]=header.split(' ');
   check(type==='blob'&&['100644','100755'].includes(mode),'qa_unexpected_tracked_source_type');
   const file=path.join(root,relative),info=await fs.lstat(file),body=await fs.readFile(file);
   const actual=createHash('sha1').update(Buffer.from('blob '+body.length+'\0')).update(body).digest('hex');
   check(info.isFile()&&!info.isSymbolicLink()&&actual===blob&&!!(info.mode&0o111)===(mode==='100755'),'qa_committed_source_bytes_or_mode_differ');
   files.push({repo,path:relative,mode,blob,bytes:body.length,sha256:sha(body)});
  }
 }
 return {repositories,files};
}
async function frozenManifest(){
 const release=phase==='desktop-stored-limits'?'issue42-stored-limits-v1':'issue42-desktop-v1';
 check(process.env.CLOUDYFAN_QA_FROZEN_RELEASE===release&&process.env.CLOUDYFAN_QA_SOURCE_MANIFEST,'qa_explicit_frozen_release_required');
 const body=await fs.readFile(process.env.CLOUDYFAN_QA_SOURCE_MANIFEST),value=JSON.parse(body);
 check(value.schema_version==='qa42-reviewed-source-manifest-v1'
  &&JSON.stringify(value.repositories)===JSON.stringify(report.source_before.repositories)
  &&JSON.stringify(value.files)===JSON.stringify(report.source_before.files),'qa_reviewed_source_manifest_differ');
 return {sha256:sha(body),verified_files:value.files.length,repositories:value.repositories,scope:'Every tracked blob/mode in both reviewed commits'};
}
function track(){
 const rows=execFileSync('/bin/ps',['-axo','pid=,ppid=,comm='],{encoding:'utf8'}).trim().split('\n').map(line=>line.trim().split(/\s+/));
 const ids=new Set([process.pid]);let changed=true;
 while(changed){changed=false;for(const row of rows)if(ids.has(Number(row[1]))&&!ids.has(Number(row[0]))){ids.add(Number(row[0]));changed=true;}}
 for(const pid of ids)if(pid!==process.pid)owned.add(pid);
}
function gone(pid){try{process.kill(pid,0);return false;}catch(error){return error.code==='ESRCH';}}
async function startRecordedFixture(command,args,{cwd,env}){
 // Preserve qa_fixture_ipc's 15s response and 5s/1s/1s exit guards, plus raw private streams.
 const stdout=await fs.open(path.join(OUT,'fixture.stdout.raw.log'),'wx',0o600);
 const stderr=await fs.open(path.join(OUT,'fixture.stderr.raw.log'),'wx',0o600);
 const child=spawn(command,args,{cwd,env,stdio:['pipe','pipe','pipe']});owned.add(child.pid);
 const reader=readline.createInterface({input:child.stdout}),queue=[],waiters=[],writes=[];
 let closed,failure,stderrPresent=false;
 const fail=message=>{failure||=Error(message);for(const waiter of waiters.splice(0)){clearTimeout(waiter.timer);waiter.reject(failure);}};
 const exit=new Promise(resolve=>child.once('close',(code,signal)=>{closed={code,signal};fail('Fixture process closed');resolve(closed);}));
 child.once('error',()=>fail('Fixture could not start'));child.stdin.on('error',()=>fail('Fixture input unavailable'));
 child.stdout.on('data',body=>writes.push(stdout.write(body)));
 child.stderr.on('data',body=>{stderrPresent=true;writes.push(stderr.write(body));});
 reader.on('line',line=>{
  let value;try{value=JSON.parse(line);}catch{fail('Fixture message invalid');return;}if(failure)return;
  const waiter=waiters.shift();if(waiter){clearTimeout(waiter.timer);waiter.resolve(value);}else queue.push(value);
 });
 reader.once('close',()=>fail('Fixture stdout closed'));
 const untilExit=milliseconds=>closed?Promise.resolve(closed):new Promise(resolve=>{
  const timer=setTimeout(()=>resolve(null),milliseconds);timer.unref();exit.then(value=>{clearTimeout(timer);resolve(value);});
 });
 return {
  next:()=>{
   if(queue.length)return Promise.resolve(queue.shift());if(failure)return Promise.reject(failure);
   return new Promise((resolve,reject)=>{const waiter={resolve,reject};waiter.timer=setTimeout(()=>{
    const at=waiters.indexOf(waiter);if(at!==-1)waiters.splice(at,1);reject(Error('Fixture response timeout'));
   },15000);waiter.timer.unref();waiters.push(waiter);});
  },
  send:value=>{if(failure)throw failure;child.stdin.write(JSON.stringify(value)+'\n');},
  stop:async()=>{
   if(!closed&&!child.stdin.destroyed)child.stdin.end(JSON.stringify({command:'stop'})+'\n');
   let outcome=await untilExit(5000);const timedOut=!outcome;
   if(!outcome){child.kill('SIGTERM');outcome=await untilExit(1000);}
   if(!outcome){child.kill('SIGKILL');outcome=await untilExit(1000);}
   reader.close();fail('Fixture stopped');const captures=await Promise.allSettled(writes);
   const closedFiles=await Promise.allSettled([stdout.close(),stderr.close()]);
   return {...(outcome||{code:null,signal:null}),timed_out:timedOut,cleanup_failed:!outcome,
    stderr_present:stderrPresent,capture_failed:[...captures,...closedFiles].some(row=>row.status==='rejected')};
  }
 };
}
async function step(name,action){currentStep=name;track();console.log('START '+name);await action();track();report.steps.push({name,passed:true});await save();console.log('PASS '+name);}
async function command(name,parameters={}){fixture.send({command:name,...parameters});const value=await fixture.next();check(value.type===name,'qa_host_command_type');return value;}
async function snap(name){
 await page.screenshot({path:path.join(OUT,name+'.png'),fullPage:false});
 await fs.writeFile(path.join(OUT,name+'.aria.txt'),await page.locator('body').ariaSnapshot(),{mode:0o600,flag:'wx'});
}
const privatePattern=/PRIVATE_[A-Z_]*CANARY|Bearer|receipts-primary|budget-primary\.sqlite/;
async function privacy(){
 check(!privatePattern.test(await page.locator('body').innerText())&&!privatePattern.test(await page.locator('body').ariaSnapshot()),'qa_private_dom_or_aria_exposed');
}
function safeLocation(location,origin){
 const result={same_origin:false,path:null,has_query:false,has_fragment:false,has_credentials:false,
  line:Number.isSafeInteger(location?.lineNumber)?location.lineNumber:null,column:Number.isSafeInteger(location?.columnNumber)?location.columnNumber:null};
 try{const url=new URL(location.url);result.same_origin=url.origin===origin;result.has_query=!!url.search;result.has_fragment=!!url.hash;
  result.has_credentials=!!url.username||!!url.password;
  if(result.same_origin&&/^\/(?:[a-z-]+\.(?:js|css))?$/.test(url.pathname))result.path=url.pathname;
 }catch{}
 return result;
}
async function verifyReferences(receipt){
 const names=new Set();let count=0;
 for(const rows of [receipt.http_responses,receipt.upstream_wire]){
  check(Array.isArray(rows),'qa_raw_reference_rows_missing');const sequences=[];
  for(const row of rows){
   check(Number.isSafeInteger(row.sequence)&&row.sequence>0,'qa_raw_reference_sequence_invalid');sequences.push(row.sequence);
   check(Object.hasOwn(row,'status')?['request_body','response_body','headers'].every(key=>typeof row[key]==='string'):
    ['body','headers'].every(key=>typeof row[key]==='string'),'qa_raw_reference_pair_missing');
   for(const [field,hashKey,sizeKey] of [['request_body','request_sha256','request_bytes'],['response_body','response_sha256','response_bytes'],['body','body_sha256','body_bytes'],['headers','headers_sha256','headers_bytes']]){
    if(!Object.hasOwn(row,field))continue;
    const name=row[field];check(typeof name==='string'&&path.basename(name)===name&&!names.has(name),'qa_raw_reference_duplicate_or_escape');names.add(name);
    const file=path.join(OUT,'host',name),info=await fs.lstat(file),body=await fs.readFile(file);
    check(info.isFile()&&!info.isSymbolicLink()&&info.nlink===1&&(info.mode&0o077)===0,'qa_raw_reference_not_private_regular');
    check(body.length===row[sizeKey]&&sha(body)===row[hashKey],'qa_raw_reference_bytes_or_sha_differ');
    check(field!=='response_body'||body.length>0||row.method==='HEAD'||[204,304].includes(row.status),'qa_raw_response_empty');count++;
   }
  }
  sequences.sort((a,b)=>a-b);check(sequences.every((value,index)=>value===index+1),'qa_raw_reference_sequence_gap');
 }
 return {status:'verified',checked_references:count,unique_names:names.size,ordering:'recorder reservation only, not native HTTP order'};
}
async function expectReport(id){await page.locator('[data-comparison-id="'+id+'"]').waitFor();await privacy();}
const reportPath=id=>'/monitor/comparisons/'+id;
const taskBox=run=>page.locator('label.comparison-task').filter({hasText:run}).getByRole('checkbox');
async function selectReport(id){
 const response=page.waitForResponse(row=>new URL(row.url()).pathname===reportPath(id)&&row.status()===200,{timeout:15000});
 await page.locator('#comparison-report-select').selectOption(id);const actual=await response;await expectReport(id);return actual;
}
async function createReport({cancel=false}={}){
 const before=report.requests.filter(row=>row.method==='POST'&&row.path==='/monitor/comparisons').length;
 await page.locator('#comparison-start').click();await page.locator('#confirm[open]').waitFor();
 if(cancel){await page.locator('#cancel-confirm').click();check(report.requests.filter(row=>row.method==='POST'&&row.path==='/monitor/comparisons').length===before,'qa_cancel_sent_comparison');return null;}
 const pending=page.waitForResponse(row=>row.request().method()==='POST'&&new URL(row.url()).pathname==='/monitor/comparisons',{timeout:15000});
 await page.locator('#apply-confirm').click();const response=await pending;
 check(response.status()===200,'qa_comparison_post_failed');
 const body=await response.body(),value=JSON.parse(body);check(/^cmp-[a-f0-9]{32}$/.test(value.comparison_id),'qa_independent_comparison_id_missing');
 check(Object.keys(response.request().postDataJSON()).sort().join(',')==='policy_version,tasks','qa_browser_sent_extra_comparison_fields');
 await expectReport(value.comparison_id);report.saved_reports||=[];report.saved_reports.push({comparison_id:value.comparison_id,sha256:sha(body),bytes:body.length,status:value.status});
 return {value,body};
}
async function desktop(ready){
 let first,mixed,repeated;
 await step('direct comparison catalogue while original model and full history are held',async()=>{
  check(ready.original_completed&&ready.long_task_held&&ready.state.mock_hold_started,'qa_real_original_and_held_task_required');
  await page.getByRole('heading',{name:'保存證據比較',exact:true}).waitFor();
  await taskBox('qa42-original-task').waitFor();await command('await-history-hold');
  check(!report.network.some(row=>row.path==='/monitor/snapshot'&&row.status===200),'qa_full_history_not_still_held');
  check(await page.locator('#comparison-policy').inputValue()===''&&await page.locator('input[data-comparison-select]:checked').count()===0,'qa_scope_or_policy_preselected');
  await command('read-baseline');await privacy();await snap('comparison-direct-catalogue');
 });
 await step('explicit original-policy scope cancel and create one immutable report',async()=>{
  await page.locator('#comparison-policy').selectOption('original-policy-reproduction-v1');
  await taskBox('qa42-original-task').check();await createReport({cancel:true});first=await createReport();
  check(first.value.items.length===1&&first.value.items[0].task.run_id==='qa42-original-task'
   &&first.value.items[0].reproduction.status==='reproduced'
   &&first.value.items[0].reproduction.decisions[0].selected_tier==='open_source','qa_original_policy_not_reproduced');
  check(first.value.items[0].reproduction.adopted===false&&first.value.items[0].reproduction.financial_admissibility==='not_replayed','qa_reproduction_misrepresented_as_adoption');
  await snap('comparison-original-reproduced');
 });
 await step('mixed saved report keeps the genuinely queued unstarted original unknown',async()=>{
  await taskBox('qa42-unstarted-task').check();mixed=await createReport();
  const known=mixed.value.items.find(row=>row.task.run_id==='qa42-original-task');
  const unknown=mixed.value.items.find(row=>row.task.run_id==='qa42-unstarted-task');
  check(mixed.value.counts.requested===2&&known?.reproduction.status==='reproduced'
   &&['unknown','not_evaluable','not_provided'].includes(unknown?.reproduction.status),'qa_missing_original_inputs_not_preserved');
  check(mixed.value.comparison_id!==first.value.comparison_id,'qa_report_overwrote_original');
  await snap('comparison-mixed-evidence');
 });
 await step('two natural five-second report polls preserve multiple disclosures and focus',async()=>{
  await command('release-history');
  const details=page.locator('.comparison-item details');check(await details.count()===2,'qa_mixed_item_details_missing');
  await details.nth(0).locator('summary').focus();await page.keyboard.press('Enter');
  await details.nth(1).locator('summary').focus();await page.keyboard.press('Space');
  const focused=await page.locator(':focus').evaluate(node=>node.closest('details')?.dataset.comparisonDetail),polls=[];
  for(let index=0;index<2;index++){
   await page.waitForResponse(row=>new URL(row.url()).pathname===reportPath(mixed.value.comparison_id)&&row.status()===200,{timeout:15000});polls.push(performance.now());
   check(await details.nth(0).getAttribute('open')!==null&&await details.nth(1).getAttribute('open')!==null,'qa_natural_poll_closed_details');
   check(await page.locator(':focus').evaluate(node=>node.closest('details')?.dataset.comparisonDetail)===focused,'qa_natural_poll_lost_focus');
  }
  report.natural_poll_gap_ms=polls[1]-polls[0];check(report.natural_poll_gap_ms>=4000&&report.natural_poll_gap_ms<=6500,'qa_natural_poll_period_changed');
  await privacy();await snap('comparison-natural-polls');
 });
 await step('GET reload preserves original bytes and explicit rerun creates another ID',async()=>{
  for(let index=0;index<2;index++){
   const response=await selectReport(first.value.comparison_id);check((await response.body()).equals(first.body),'qa_saved_report_get_bytes_changed');
  }
  repeated=await createReport();check(![first.value.comparison_id,mixed.value.comparison_id].includes(repeated.value.comparison_id),'qa_explicit_rerun_overwrote_saved_record');
  await selectReport(first.value.comparison_id);await command('verify-readonly');
 });
 await step('late report read cannot overwrite a more recently selected report',async()=>{
  await command('hold-read',{path:reportPath(mixed.value.comparison_id)});
  const late=page.waitForResponse(row=>new URL(row.url()).pathname===reportPath(mixed.value.comparison_id)&&row.status()===200,{timeout:15000});
  await page.locator('#comparison-report-select').selectOption(mixed.value.comparison_id);await command('await-read-hold');
  await selectReport(first.value.comparison_id);await command('release-read');await (await late).finished();
  await page.waitForTimeout(100);check(await page.locator('[data-comparison-id]').getAttribute('data-comparison-id')===first.value.comparison_id,'qa_late_read_replaced_selected_report');
  await privacy();
 });
 await step('logout and newly verified operator fence the old successful report read',async()=>{
  await command('hold-read',{path:reportPath(mixed.value.comparison_id)});
  const late=page.waitForResponse(row=>new URL(row.url()).pathname===reportPath(mixed.value.comparison_id)&&row.status()===200,{timeout:15000});
  await page.locator('#comparison-report-select').selectOption(mixed.value.comparison_id);await command('await-read-hold');
  await page.locator('#logout').click();check(await page.locator('[data-comparison-id]').count()===0,'qa_logout_kept_saved_report');
  const rotated=await command('rotate-operator');await page.locator('#token').fill(rotated.operator_token);
  await page.locator('#login-form button[type="submit"]').click();await taskBox('qa42-original-task').waitFor();
  check(await page.locator('#comparison-policy').inputValue()===''&&await page.locator('input[data-comparison-select]:checked').count()===0,'qa_new_operator_inherited_scope');
  await selectReport(first.value.comparison_id);await command('release-read');await (await late).finished();
  await page.waitForTimeout(100);check(await page.locator('[data-comparison-id]').getAttribute('data-comparison-id')===first.value.comparison_id,'qa_old_session_replaced_new_operator_report');
  await snap('comparison-new-operator');await command('verify-readonly');
 });
 await step('saved mixed report stays immutable after the separate held original completes',async()=>{
  await command('release-model');const finished=(await command('state')).state;
  check(finished.model_calls===ready.state.model_calls&&finished.source_requests.length===0&&finished.accounting_requests.length===0,'qa_extra_model_source_or_accounting_request');
  await command('read-baseline');const response=await selectReport(mixed.value.comparison_id);
  check((await response.body()).equals(mixed.body),'qa_saved_unknown_recomputed_after_original_finish');
  await command('verify-readonly');await privacy();await snap('comparison-immutable-after-original-finish');
 });
 await step('no unexpected browser diagnostics or rejection',async()=>{
  check(report.blocked.length===0&&report.page_errors.length===0&&!report.network.some(row=>row.status>=400),'qa_unexpected_browser_request_or_error');
  check(!report.console.some(row=>['warning','error'].includes(row.level)),'qa_browser_console_warning_or_error');
  check(!privatePattern.test(JSON.stringify([report.console,report.page_errors])),'qa_private_browser_diagnostic_exposed');
 });
}
async function storedLimitsDesktop(ready){
 let legacy,baseline=false,failure;
 const posts=()=>report.requests.filter(row=>row.method==='POST'&&row.path==='/monitor/comparisons').length;
 try{
  await step('legal frozen two-task report remains readable with its own limits',async()=>{
   check(ready.original_completed&&ready.long_task_held&&ready.state.mock_hold_started,'qa_real_original_and_held_task_required');
   await page.getByRole('heading',{name:'保存證據比較',exact:true}).waitFor();await taskBox('qa42-original-task').waitFor();
   await command('await-history-hold');await command('release-history');await command('read-baseline');baseline=true;
   legacy=await command('seed-old-comparison-limits');
   check(legacy.frozen_limits.max_tasks===2&&legacy.requested_tasks===2&&legacy.original_state_unchanged,'qa_legacy_limits_source_missing');
   report.legacy_report={comparison_id:legacy.comparison_id,sha256:legacy.response_sha256,bytes:legacy.response_bytes,limits:legacy.frozen_limits};
   await page.locator('#comparison-report-select option[value="'+legacy.comparison_id+'"]').waitFor({state:'attached',timeout:15000});
   const response=await selectReport(legacy.comparison_id),body=await response.body(),value=JSON.parse(body);
   check(sha(body)===legacy.response_sha256&&body.length===legacy.response_bytes&&value.limits.max_tasks===2
    &&value.scope.tasks.length===2,'qa_legacy_limits_report_bytes_changed');
   await snap('stored-limits-legacy-report');
  });
  await step('current admission shrink preserves old selection and rejects an outdated confirmation',async()=>{
   await page.locator('#comparison-policy').selectOption('original-policy-reproduction-v1');
   await taskBox('qa42-original-task').check();await taskBox('qa42-unstarted-task').check();
   const before=posts();await page.locator('#comparison-start').click();await page.locator('#confirm[open]').waitFor();
   const next=page.waitForResponse(async row=>row.request().method()==='GET'&&new URL(row.url()).pathname==='/monitor/comparisons'
    &&row.status()===200&&(await row.json()).limits.max_tasks===1,{timeout:15000});
   const tightened=await command('tighten-comparison-creation-limit');check(tightened.creation_limits.max_tasks===1,'qa_creation_limit_not_tightened');
   await next;
   await page.waitForFunction(()=>document.querySelector('#comparison-start')?.disabled===true
    &&document.querySelector('#comparison-policy')?.disabled===false,{},{timeout:6000});
   check(await page.locator('input[data-comparison-select]:checked').count()===2,'qa_limit_change_silently_changed_selection');
   await page.locator('#apply-confirm').click();await page.locator('#confirm').waitFor({state:'hidden'});
   check(posts()===before,'qa_outdated_confirmation_sent_comparison');
   const response=await selectReport(legacy.comparison_id);
   check(sha(await response.body())===legacy.response_sha256,'qa_old_report_changed_after_current_limit_shrank');
   await privacy();await snap('stored-limits-current-cap-one');
  });
  await step('new single-task comparison follows current admission without changing original authority',async()=>{
   await taskBox('qa42-unstarted-task').uncheck();const created=await createReport();
   check(created.value.scope.tasks.length===1&&created.value.counts.requested===1&&created.value.limits.max_tasks===1
    &&created.value.comparison_id!==legacy.comparison_id,'qa_new_report_ignored_current_creation_limit');
   const response=await selectReport(legacy.comparison_id);
   check(sha(await response.body())===legacy.response_sha256,'qa_new_report_overwrote_old_limits_report');
   await privacy();await snap('stored-limits-new-report-and-old-read');
  });
  await step('directed limits flow has no unexpected browser diagnostics or rejection',async()=>{
   check(report.blocked.length===0&&report.page_errors.length===0&&!report.network.some(row=>row.status>=400),'qa_unexpected_browser_request_or_error');
   check(!report.console.some(row=>['warning','error'].includes(row.level)),'qa_browser_console_warning_or_error');
   check(!privatePattern.test(JSON.stringify([report.console,report.page_errors])),'qa_private_browser_diagnostic_exposed');
  });
 }catch(error){failure=error;}
 finally{
  for(const name of [...(baseline?['verify-readonly']:[]),'release-model']){
   try{await command(name);}catch(error){report.directed_cleanup_failures||=[];report.directed_cleanup_failures.push(name);failure||=error;}
  }
 }
 if(failure)throw failure;
}
async function main(){
 process.umask(0o077);check(process.argv[2]&&CORE&&process.env.CLOUDYFAN_QA_PYTHON,'qa_explicit_paths_required');
 check(['desktop-frozen','desktop-stored-limits'].includes(phase),'qa_phase_not_released');await fs.mkdir(OUT,{recursive:false,mode:0o700});await save();
 report.source_before=await sourceManifest();report.frozen_manifest=await frozenManifest();await save();
 fixture=await startRecordedFixture(process.env.CLOUDYFAN_QA_PYTHON,[path.join(ROOT,'scripts/qa_saved_comparisons_http.py'),
  '--output',path.join(OUT,'host'),'--phase',phase],{cwd:ROOT,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONNOUSERSITE:'1',UV_OFFLINE:'1',
   PYTHONPATH:[path.join(ROOT,'src'),path.join(CORE,'src')].join(path.delimiter)}});
 const ready=await fixture.next();check(ready.type==='ready'&&ready.synthetic===true,'qa_real_host_ready_missing');
 report.fixture_pid=ready.fixture_pid;report.origin=ready.origin;owned.add(ready.fixture_pid);
 const {chromium}=require(process.env.CLOUDYFAN_PLAYWRIGHT_MODULE||'/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
 browser=await chromium.launch({channel:'chrome',headless:false});report.browser=browser.version();track();
 const context=await browser.newContext({viewport:{width:1280,height:900},locale:'zh-TW',serviceWorkers:'block'});
 await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin===ready.origin&&(request.method()==='GET'||request.method()==='POST'&&url.pathname==='/monitor/comparisons'))await route.continue();
  else{report.blocked.push({method:request.method(),same_origin:url.origin===ready.origin});await route.abort();}
 });
 page=await context.newPage();page.setDefaultTimeout(6000);track();
 const ids=new WeakMap();let nextId=0;
 page.on('request',request=>{const id=++nextId;ids.set(request,id);report.requests.push({request_id:id,method:request.method(),path:new URL(request.url()).pathname,at_ms:performance.now()});});
 page.on('response',response=>report.network.push({request_id:ids.get(response.request()),method:response.request().method(),path:new URL(response.url()).pathname,status:response.status(),at_ms:performance.now()}));
 page.on('console',message=>report.console.push({level:message.type(),text:message.text(),location:safeLocation(message.location(),ready.origin),at_ms:performance.now()}));
 page.on('pageerror',error=>report.page_errors.push({name:error.name,message:error.message}));
 await page.goto(ready.origin+'/#comparisons',{waitUntil:'domcontentloaded'});
 if(phase==='desktop-stored-limits')await storedLimitsDesktop(ready);else await desktop(ready);
 report.passed=true;
}
function runCli(){return main().catch(async error=>{
 report.passed=false;report.failure={step:currentStep,code:error.qaCode||'qa_desktop_failed'};process.exitCode=1;
 const body=String(error.stack||error)+'\n';await fs.writeFile(path.join(OUT,'desktop-exception.txt'),body,{mode:0o600,flag:'wx'});
 report.failure.original_private_reference={path:'desktop-exception.txt',sha256:sha(body),bytes:Buffer.byteLength(body)};
 if(page)await snap('desktop-failure').catch(()=>{report.failure_capture_failed=true;});
}).finally(async()=>{
 track();if(browser)try{await browser.close();}catch{report.browser_close_failed=true;process.exitCode=1;}
 if(fixture){report.fixture_exit=await fixture.stop();if(report.fixture_exit.code!==0||report.fixture_exit.signal||report.fixture_exit.timed_out||report.fixture_exit.stderr_present||report.fixture_exit.capture_failed){report.cleanup_failed=true;process.exitCode=1;}}
 try{
  const receipt=JSON.parse(await fs.readFile(path.join(OUT,'host','fixture-receipt.json')));
  report.host_verification={artifacts:receipt.artifact_verification,upstream:receipt.upstream_artifact_verification,
   readonly_oracle_verified:receipt.readonly_oracle_verified,held_model_completed:receipt.held_model_completed,
   unstarted_task_still_queued:receipt.unstarted_task_still_queued,
   blocked_socket_attempt_count:receipt.blocked_socket_attempt_count,cleanup:receipt.cleanup,tool_scope:receipt.tool_scope};
  report.independent_reference_verification=await verifyReferences(receipt);
  check(receipt.artifact_verification.status==='verified'&&receipt.upstream_artifact_verification.status==='verified'
   &&receipt.readonly_oracle_verified&&receipt.held_model_completed&&receipt.unstarted_task_still_queued&&receipt.blocked_socket_attempt_count===0
   &&Object.values(receipt.cleanup).every(Boolean),'qa_host_cleanup_or_evidence_failed');
 }catch{report.host_verification_failed=true;process.exitCode=1;}
 const end=performance.now()+5000;while([...owned].some(pid=>!gone(pid))&&performance.now()<end)await new Promise(resolve=>setTimeout(resolve,100));
 report.owned_child_pids_exited=[...owned].map(pid=>({pid,exited:gone(pid)}));
 if(report.owned_child_pids_exited.some(row=>!row.exited)){report.cleanup_failed=true;process.exitCode=1;}
 try{report.source_after=await sourceManifest();report.source_unchanged=JSON.stringify(report.source_before)===JSON.stringify(report.source_after);
  if(!report.source_unchanged)process.exitCode=1;
 }catch{report.source_readback_failed=true;process.exitCode=1;}
 report.finished_at=new Date().toISOString();await save();console.log(JSON.stringify({passed:report.passed,failure:report.failure,report:path.join(OUT,'desktop-report.json')}));
});}
module.exports={sourceManifest};
if(require.main===module)void runCli();
