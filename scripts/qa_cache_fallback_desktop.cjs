#!/usr/bin/env node
'use strict';
// Headed desktop QA for #49 cache fallback and disable. Synthetic host only.
const fs=require('node:fs/promises');
const path=require('node:path');
const {spawn}=require('node:child_process');
const readline=require('node:readline');
const ROOT=path.resolve(__dirname,'..'),CORE=process.env.CLOUDYFAN_CORE_ROOT;
const OUT=path.resolve(process.argv[2]||'__MISSING_OUTPUT__');
const report={schema_version:'qa49-cache-fallback-desktop-v1',synthetic:true,
 scope:'Actual headed Chrome 1280x900 and real main-service HTTP/Core/Budget/Store/Identity; upstreams MockTransport only',
 started_at:new Date().toISOString(),steps:[],requests:[],console:[],page_errors:[],blocked:[]};
let child,lines,browser,page;
function check(value,code){if(!value){const error=Error(code);error.qaCode=code;throw error;}}
async function save(){await fs.writeFile(path.join(OUT,'desktop-report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});}
async function next(){
 const timer=new Promise((_,reject)=>setTimeout(()=>reject(Error('qa_fixture_response_timeout')),15000));
 return Promise.race([new Promise((resolve,reject)=>{lines.once('line',line=>resolve(JSON.parse(line)));child.once('close',()=>reject(Error('qa_fixture_closed')));}),timer]);
}
async function command(name){child.stdin.write(JSON.stringify({command:name})+'\n');return next();}
async function step(name,body){console.log('START '+name);await body();report.steps.push({name,passed:true});console.log('PASS '+name);await save();}
async function snap(name){await page.screenshot({path:path.join(OUT,name+'.png'),fullPage:true});}
async function main(){
 process.umask(0o077);check(process.argv[2]&&CORE&&process.env.CLOUDYFAN_QA_PYTHON,'qa_explicit_paths_required');
 await fs.mkdir(OUT,{recursive:false,mode:0o700});await save();
 child=spawn(process.env.CLOUDYFAN_QA_PYTHON,[path.join(ROOT,'scripts/qa_cache_fallback_http.py'),'--output',path.join(OUT,'host')],
  {cwd:ROOT,stdio:['pipe','pipe','pipe'],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONNOUSERSITE:'1',UV_OFFLINE:'1',
   PYTHONPATH:[path.join(ROOT,'src'),path.join(CORE,'src')].join(path.delimiter)}});
 const stderr=await fs.open(path.join(OUT,'fixture.stderr.raw.log'),'wx',0o600);child.stderr.on('data',chunk=>void stderr.write(chunk));
 lines=readline.createInterface({input:child.stdout});
 const ready=await next();check(ready.type==='ready'&&ready.synthetic===true,'qa_real_host_ready_missing');
 report.origin=ready.origin;report.baseline=ready.state;
 const {chromium}=require(process.env.CLOUDYFAN_PLAYWRIGHT_MODULE||'/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
 browser=await chromium.launch({channel:'chrome',headless:false});report.browser=browser.version();
 const context=await browser.newContext({viewport:{width:1280,height:900},locale:'zh-TW',serviceWorkers:'block'});
 await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  const allowed=url.origin===ready.origin&&(request.method()==='GET'||request.method()==='POST'&&url.pathname==='/monitor/cache-affinity/activate');
  if(allowed)await route.continue();else{report.blocked.push({method:request.method(),same_origin:url.origin===ready.origin});await route.abort();}
 });
 page=await context.newPage();page.setDefaultTimeout(8000);
 page.on('request',request=>report.requests.push({method:request.method(),path:new URL(request.url()).pathname}));
 page.on('console',message=>report.console.push({level:message.type(),text:message.text().slice(0,300)}));
 page.on('pageerror',error=>report.page_errors.push({name:error.name,message:error.message.slice(0,300)}));
 await page.goto(ready.origin+'/#cache',{waitUntil:'domcontentloaded'});
 if(await page.locator('#login[open]').count()){await page.locator('#token').fill(ready.operator_token);await page.locator('#login-form button[type="submit"]').click();}
 let pair,expired,disabled;
 const apply=async version=>{
  await page.locator('#cache-affinity-version').selectOption(version);
  await page.locator('#cache-affinity-apply').click();await page.locator('#confirm[open]').waitFor();
  const pending=page.waitForResponse(row=>row.request().method()==='POST'&&new URL(row.url()).pathname==='/monitor/cache-affinity/activate',{timeout:12000});
  await page.locator('#apply-confirm').click();const response=await pending;check(response.status()===200,'qa_affinity_apply_failed');
  return response.json();
 };
 const open=async run=>{
  await page.reload({waitUntil:'domcontentloaded'});
  await page.locator(`#cache-task option[value="studio/${run}"]`).waitFor({state:'attached',timeout:12000});
  const pending=page.waitForResponse(row=>new URL(row.url()).pathname==='/monitor/cache-observations/studio/'+run,{timeout:12000});
  await page.locator('#cache-task').selectOption('studio/'+run);return (await pending).json();
 };
 await step('adopt the policy and record an adopted tie-break',async()=>{
  await page.goto(ready.origin+'/#cache',{waitUntil:'domcontentloaded'});
  await page.locator('[data-affinity-active="none"]').waitFor();
  check((await apply('prefix-affinity-v1')).active_version==='prefix-affinity-v1','qa_affinity_not_activated');
  pair=await command('create-pair');
  check(pair.second_model===ready.models['open_source-1'],'qa_affinity_not_adopted');
 });
 await step('expired observation falls back to the original order with its own reason',async()=>{
  expired=await command('expire-and-create');
  check(expired.model===ready.models['open_source-0'],'qa_expired_did_not_fall_back');
  const detail=await open('qa49-expired');
  const decision=detail.affinity.decisions.find(row=>row.decision_key==='0:open_source');
  check(decision.applied.adopted===false&&decision.applied.reason==='outside_local_hint_window','qa_expired_reason_missing');
  await page.locator('[data-affinity-reason="outside_local_hint_window"]').first().waitFor();await snap('fallback-expired');
 });
 await step('disabling affects only new tasks and old reasons stay',async()=>{
  check((await apply('')).active_version===null,'qa_affinity_not_disabled');
  disabled=await command('create-after-disable');
  check(disabled.model===ready.models['open_source-0'],'qa_disabled_reordered');
  const fresh=await open('qa49-disabled');check(fresh.affinity.frozen_version===null&&fresh.affinity.decisions.length===0,'qa_disabled_task_has_affinity');
  await page.locator('[data-affinity-frozen="none"]').waitFor();
  const old=await open('qa49-second');
  check(old.affinity.frozen_version==='prefix-affinity-v1'&&old.affinity.decisions.find(row=>row.decision_key==='0:open_source').applied.adopted===true,'qa_old_reason_changed');
  await page.locator('[data-affinity-adopted="true"]').first().waitFor();await snap('fallback-disabled-old-kept');
 });
 await step('reads and polling add no model, source, budget or original-file change',async()=>{
  await page.waitForTimeout(5500);
  const result=await command('state');
  check(JSON.stringify(result.state)===JSON.stringify(disabled.state),'qa_fallback_read_changed_original_state');
 });
 await step('no page errors or prompt leak',async()=>{
  check(!report.blocked.length&&!report.page_errors.length&&!report.console.some(row=>row.level==='error'),'qa_browser_diagnostics');
  check(!(await page.locator('#cache-page').innerText()).includes('PRIVATE_QA49_CANARY'),'qa_page_leaks_prompt');
 });
 report.passed=true;
}
main().catch(async error=>{report.passed=false;report.failure={code:error.qaCode||'qa_desktop_failed',message:String(error.message).slice(0,300)};process.exitCode=1;
 if(page)await snap('desktop-failure').catch(()=>{});
}).finally(async()=>{
 if(browser)await browser.close().catch(()=>{report.browser_close_failed=true;});
 if(child){child.stdin.write(JSON.stringify({command:'stop'})+'\n');
  report.fixture_exit=await new Promise(resolve=>{const timer=setTimeout(()=>{child.kill('SIGKILL');resolve({timed_out:true});},10000);
   child.once('close',(code,signal)=>{clearTimeout(timer);resolve({code,signal});});});
  if(report.fixture_exit.code!==0)process.exitCode=1;}
 try{report.host=JSON.parse(await fs.readFile(path.join(OUT,'host','fixture-receipt.json')));
  if(!report.host.readonly_verified||report.host.blocked_socket_attempt_count!==0)process.exitCode=1;}catch{report.host_receipt_missing=true;process.exitCode=1;}
 report.finished_at=new Date().toISOString();await save();
 console.log(JSON.stringify({passed:report.passed&&process.exitCode!==1,report:path.join(OUT,'desktop-report.json')}));
});
