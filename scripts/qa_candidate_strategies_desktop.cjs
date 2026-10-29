#!/usr/bin/env node
'use strict';
// Headed desktop QA for registered candidate strategies (#43). Synthetic host only.
const fs=require('node:fs/promises');
const path=require('node:path');
const {spawn}=require('node:child_process');
const readline=require('node:readline');
const ROOT=path.resolve(__dirname,'..'),CORE=process.env.CLOUDYFAN_CORE_ROOT;
const OUT=path.resolve(process.argv[2]||'__MISSING_OUTPUT__');
const report={schema_version:'qa43-candidate-strategies-desktop-v1',synthetic:true,
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
async function create(strategy){
 await page.locator('#comparison-strategy').selectOption(strategy);
 await page.locator('#comparison-start').click();await page.locator('#confirm[open]').waitFor();
 const pending=page.waitForResponse(row=>row.request().method()==='POST'&&new URL(row.url()).pathname==='/monitor/comparisons',{timeout:15000});
 await page.locator('#apply-confirm').click();const response=await pending;
 check(response.status()===200,'qa_comparison_post_failed');
 const sent=response.request().postDataJSON();
 check(Object.keys(sent).sort().join(',')==='candidate_strategy,policy_version,tasks'&&sent.candidate_strategy===strategy,'qa_browser_sent_unexpected_fields');
 const value=await response.json();
 await page.locator(`[data-comparison-id="${value.comparison_id}"]`).waitFor();
 await page.locator(`[data-candidate-strategy="${strategy}"]`).waitFor();
 return value;
}
async function main(){
 process.umask(0o077);check(process.argv[2]&&CORE&&process.env.CLOUDYFAN_QA_PYTHON,'qa_explicit_paths_required');
 await fs.mkdir(OUT,{recursive:false,mode:0o700});await save();
 child=spawn(process.env.CLOUDYFAN_QA_PYTHON,[path.join(ROOT,'scripts/qa_candidate_strategies_http.py'),'--output',path.join(OUT,'host')],
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
  const allowed=url.origin===ready.origin&&(request.method()==='GET'||request.method()==='POST'&&url.pathname==='/monitor/comparisons');
  if(allowed)await route.continue();else{report.blocked.push({method:request.method(),same_origin:url.origin===ready.origin});await route.abort();}
 });
 page=await context.newPage();page.setDefaultTimeout(8000);
 page.on('request',request=>report.requests.push({method:request.method(),path:new URL(request.url()).pathname}));
 page.on('console',message=>report.console.push({level:message.type(),text:message.text().slice(0,300)}));
 page.on('pageerror',error=>report.page_errors.push({name:error.name,message:error.message.slice(0,300)}));
 await page.goto(ready.origin+'/#comparisons',{waitUntil:'domcontentloaded'});
 if(await page.locator('#login[open]').count()){await page.locator('#token').fill(ready.operator_token);await page.locator('#login-form button[type="submit"]').click();}
 const box=page.locator(`input[data-comparison-select='${JSON.stringify(['studio','qa43-classified'])}']`);
 await step('catalogue lists only registered strategies and nothing is preselected',async()=>{
  await page.getByRole('heading',{name:'保存證據比較',exact:true}).waitFor();await box.waitFor();
  const options=await page.locator('#comparison-strategy option').evaluateAll(rows=>rows.map(row=>row.value));
  check(JSON.stringify(options)===JSON.stringify(['','qcs-classify-v1','qcs-direct-v1']),'qa_unregistered_strategy_offered');
  check(await page.locator('#comparison-strategy').inputValue()===''&&await page.locator('#comparison-policy').inputValue()==='','qa_strategy_or_policy_preselected');
  await snap('candidate-catalogue');
 });
 let direct,classify;
 await step('direct strategy stops at the first divergence from the saved classification',async()=>{
  await page.locator('#comparison-policy').selectOption('original-policy-reproduction-v1');await box.check();
  direct=await create('qcs-direct-v1');const item=direct.items[0];
  check(item.candidate.status==='diverged'&&item.candidate.adopted===false,'qa_direct_divergence_missing');
  check(item.candidate.decisions[1].stop==='requires_unsaved_alternative_answer'&&item.candidate.decisions[2].status==='not_observed','qa_first_divergence_not_stopped');
  await page.locator('[data-candidate-status="diverged"]').waitFor();
  await page.getByText('需要未保存的替代模型答案，推演在此停止。').first().waitFor();
  await page.getByText('原成本模型預測（非實付）',{exact:false}).first().waitFor();
  await snap('candidate-direct-diverged');
 });
 await step('classify strategy reuses the same saved classification without divergence',async()=>{
  classify=await create('qcs-classify-v1');
  check(classify.items[0].candidate.status==='same_as_original'&&classify.comparison_id!==direct.comparison_id,'qa_classify_candidate_unexpected');
  await page.locator('[data-candidate-status="same_as_original"]').waitFor();await snap('candidate-classify-same');
 });
 await step('candidate reports add no model, source, budget or original-file change',async()=>{
  const result=await command('state');
  check(JSON.stringify(result.state)===JSON.stringify(ready.state),'qa_candidate_changed_original_state');
  check(result.customer_report_denied===true,'qa_customer_read_global_comparisons');
 });
 await step('no unexpected browser diagnostics, prompt leak or adoption',async()=>{
  check(!report.blocked.length&&!report.page_errors.length&&!report.console.some(row=>row.level==='error'),'qa_browser_diagnostics');
  const text=await page.locator('#comparisons-page').innerText();
  check(!text.includes('PRIVATE_QA43_CANARY')&&[direct,classify].every(row=>row.items.every(item=>item.candidate.adopted===false)),
   'qa_page_leaks_prompt_or_adopts');
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
