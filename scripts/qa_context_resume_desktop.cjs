#!/usr/bin/env node
'use strict';
// Headed desktop QA for #52 frozen preparation across escalation and corruption. Synthetic host only.
const fs=require('node:fs/promises');
const path=require('node:path');
const {spawn}=require('node:child_process');
const readline=require('node:readline');
const ROOT=path.resolve(__dirname,'..'),CORE=process.env.CLOUDYFAN_CORE_ROOT;
const OUT=path.resolve(process.argv[2]||'__MISSING_OUTPUT__');
const report={schema_version:'qa52-context-resume-desktop-v1',synthetic:true,
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
 child=spawn(process.env.CLOUDYFAN_QA_PYTHON,[path.join(ROOT,'scripts/qa_context_resume_http.py'),'--output',path.join(OUT,'host')],
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
  const allowed=url.origin===ready.origin&&request.method()==='GET';
  if(allowed)await route.continue();else{report.blocked.push({method:request.method(),same_origin:url.origin===ready.origin});await route.abort();}
 });
 page=await context.newPage();page.setDefaultTimeout(8000);
 page.on('request',request=>report.requests.push({method:request.method(),path:new URL(request.url()).pathname}));
 page.on('console',message=>report.console.push({level:message.type(),text:message.text().slice(0,300)}));
 page.on('pageerror',error=>report.page_errors.push({name:error.name,message:error.message.slice(0,300)}));
 await page.goto(ready.origin+'/#cache',{waitUntil:'domcontentloaded'});
 if(await page.locator('#login[open]').count()){await page.locator('#token').fill(ready.operator_token);await page.locator('#login-form button[type="submit"]').click();}
 const pick=async run=>{
  const pending=page.waitForResponse(row=>new URL(row.url()).pathname==='/monitor/cache-observations/studio/'+run,{timeout:12000});
  await page.locator('#cache-task').selectOption('studio/'+run);const response=await pending;
  check(response.status()===200,'qa_cache_detail_failed');return response.json();
 };
 const prep=async run=>{
  const pending=page.waitForResponse(row=>new URL(row.url()).pathname==='/monitor/context-preparations/studio/'+run,{timeout:12000});
  await page.locator('#cache-task').selectOption('studio/'+run);const response=await pending;
  check(response.status()===200,'qa_preparation_detail_failed');return {value:await response.json(),text:await response.text()};
 };
 await step('cache page lists the synthetic tasks',async()=>{
  await page.goto(ready.origin+'/#cache',{waitUntil:'domcontentloaded'});
  await page.locator('#cache-task option[value="studio/qa52-tamper"]').waitFor({state:'attached'});
 });
 let escalate,tamper;
 await step('every dispatch after escalation carries the same frozen content',async()=>{
  escalate=await prep('qa52-escalate');const p=escalate.value.preparation;
  check(p.generation_requests>=2&&p.dispatches.every(row=>row.matches_prepared===true),'qa_escalation_unexpected');
  await page.locator('[data-dispatch-matches="true"]').nth(1).waitFor();await snap('resume-escalation');
 });
 await step('corrupted frozen preparation is shown as unverifiable',async()=>{
  tamper=await prep('qa52-tamper');check(tamper.value.status==='unverifiable'&&tamper.value.preparation===null,'qa_tamper_not_detected');
  await page.locator('[data-preparation-status="unverifiable"]').waitFor();await snap('resume-unverifiable');
 });
 await step('reads add no model, source, budget or original-file change',async()=>{
  const result=await command('state');
  check(JSON.stringify(result.state)===JSON.stringify(ready.state),'qa_resume_read_changed_original_state');
 });
 await step('no page errors and no original text leak',async()=>{
  check(!report.blocked.length&&!report.page_errors.length&&!report.console.some(row=>row.level==='error'),'qa_browser_diagnostics');
  const text=await page.locator('#cache-page').innerText();
  check(!/PRIVATE_QA52/.test(text)&&!/PRIVATE_QA52/.test(escalate.text)&&!/PRIVATE_QA52/.test(tamper.text),'qa_page_leaks_private_text');
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
