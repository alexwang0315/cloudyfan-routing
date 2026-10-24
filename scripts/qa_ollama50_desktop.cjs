/* Desktop acceptance for the named, mock-only Ollama customer fixture. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require(process.env.CLOUDYFAN_PLAYWRIGHT_MODULE ||
  '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const origin = new URL(process.argv[2]).origin;
const output = process.argv[3];
const observeExisting = process.argv[4] === '--observe-existing';
const report = {schema_version:1,viewport:{width:1280,height:800},verified:false,
  real_supplier_requests:0,checks:[],page_errors:[],blocked_requests:[],request_failures:[],responses:[]};
let browser, page;
async function check(name, action) { await action(); report.checks.push({name,passed:true}); }
async function snap(name) {
  await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
  await fs.writeFile(path.join(output,name+'.aria.txt'),await page.locator('body').ariaSnapshot(),{mode:0o600});
}
async function until(read,predicate,message,timeout=30000,interval=1000) {
  const deadline=Date.now()+timeout;
  while(Date.now()<deadline) {
    const value=await read();
    if(predicate(value)) return value;
    await new Promise(resolve=>setTimeout(resolve,interval));
  }
  assert.fail(message);
}

(async()=>{
  const target=new URL(origin);
  assert.equal(target.protocol,'http:');assert.equal(target.hostname,'127.0.0.1');
  assert.notEqual(target.port,'18767','Preserve the original preview');
  assert(output,'Provide a fresh private output directory');
  await fs.mkdir(output,{recursive:false,mode:0o700});
  browser=await chromium.launch({channel:'chrome',headless:true});
  report.browser=browser.version();
  const context=await browser.newContext({viewport:report.viewport,locale:'zh-TW'});
  await context.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin){report.blocked_requests.push(url.origin+url.pathname);await route.abort();return;}
    await route.continue();
  });
  const sessionResponse=await context.request.get(origin+'/demo/session');
  assert.equal(sessionResponse.status(),200);
  const session=await sessionResponse.json();assert.equal(session.demo,true);
  const headers={Authorization:'Bearer '+session.token};
  async function read(endpoint) {
    const response=await context.request.get(origin+endpoint,{headers});
    assert.equal(response.status(),200);return response.json();
  }
  page=await context.newPage();
  page.on('pageerror',()=>report.page_errors.push('JavaScript page error'));
  page.on('requestfailed',request=>report.request_failures.push({method:request.method(),path:new URL(request.url()).pathname}));
  page.on('response',response=>report.responses.push({method:response.request().method(),path:new URL(response.url()).pathname,status:response.status()}));
  await page.goto(origin);
  await page.locator('#simulation-banner').waitFor();
  const initial=await read('/monitor/snapshot');
  assert.equal(initial.demo,true);assert.equal(initial.simulation.customer,'ollama-customer');
  assert.equal(initial.simulation.model_upstream,'synthetic_mock_transport');
  if(!observeExisting) {
    await check('opening and reading the idle console dispatches no tasks',async()=>{
      assert.equal(initial.simulation.status,'idle');assert.equal(initial.summary.tasks,0);
      assert.equal(initial.ledger.rows.length,0);
      assert.equal(report.responses.some(r=>r.method==='POST'),false);
      await snap('01-idle');
    });
    await check('a desktop confirmation starts the SDK customer and shows intermediate progress',async()=>{
      await page.locator('#simulation-start').click();
      await page.locator('#confirm').waitFor({state:'visible'});
      assert.equal((await read('/monitor/snapshot')).summary.tasks,0);
      await page.locator('#apply-confirm').click();
      await page.locator('#confirm').waitFor({state:'hidden'});
      const progress=await until(()=>page.locator('#simulation-progress-label').innerText(),value=>{
        const number=Number(value.match(/已完成 (\d+)/)?.[1]);return number>0&&number<50;
      },'No intermediate desktop progress was observed',60000);
      report.intermediate_progress=progress;
      assert.equal(await page.locator('#simulation-start').isDisabled(),true);
      await snap('02-in-progress');
    });
  } else {
    assert.equal(initial.simulation.status,'completed','Observation mode never starts a batch');
    report.observed_existing_batch=true;
  }
  const final=await until(()=>read('/monitor/snapshot'),value=>{
    assert.notEqual(value.simulation.status,'failed','The SDK batch failed; retain original runtime evidence');
    return value.simulation.status==='completed';
  },'The SDK batch did not complete',240000,5000);
  const batch=final.simulation.batch_id;
  await check('fifty customer HTTP tasks retain all pass, fail and unknown outcomes',async()=>{
    assert.equal(final.summary.tasks,50);assert.equal(final.tasks.length,50);
    assert.equal(final.summary.passed,40);assert.equal(final.summary.failed,5);assert.equal(final.summary.unknown,5);
    assert.equal(final.summary.first_passed,10);assert.equal(final.summary.escalated,40);
    assert.equal(final.summary.jev_once,20);assert.equal(final.summary.jev_twice,20);
    assert.deepEqual(final.simulation.outcomes,{passed:40,failed:5,unverifiable:5});
    assert.equal(final.simulation.completed,50);assert.equal(final.simulation.new_model_or_jev_posts,170);
    assert.equal(final.simulation.identity_diagnostics.failures,0);
    assert.deepEqual(final.clients.map(client=>client.id),['ollama-customer']);
    for(const task of final.tasks) {
      assert.equal(task.client,'ollama-customer');assert.equal(task.ingress.protocol,'ollama');
      assert.equal(task.ingress.endpoint,'/api/chat');assert.equal(task.ingress.alias,'cloudyfan/mrc:simulation');
      assert.equal(task.trace.basis,'verified_core_result');
      assert.equal(task.trace.distributions_are_correctness_probabilities,false);
    }
    const counts={open_source:0,low:0,mid:0,high:0};
    for(const task of final.tasks)for(const attempt of task.trace.attempts)counts[attempt.tier]++;
    assert.deepEqual(counts,{open_source:50,low:15,mid:25,high:20});
    report.outcomes=final.simulation.outcomes;report.generation_attempts=counts;
  });
  const billing=await read('/monitor/billing');
  await check('supplier events and customer charges match independently calculated exact amounts',async()=>{
    assert.equal(final.ledger.rows.length,170);assert.equal(final.ledger.unknown_events,0);
    assert.equal(final.ledger.rows.filter(row=>row.request_kind==='generation').length,110);
    assert.equal(final.ledger.rows.filter(row=>row.request_kind==='classification').length,60);
    assert.equal(final.ledger.known_usd,'0.1155');assert.equal(final.ledger.total_usd,'0.1155');
    assert.equal(billing.demo,true);assert.equal(billing.source.configured,false);
    assert.equal(billing.entries.length,171);assert.equal(billing.entries.filter(entry=>entry.kind==='debit').length,170);
    assert.equal(billing.entries.filter(entry=>entry.kind==='credit').length,1);assert.deepEqual(billing.pending,[]);
    assert.equal(billing.clients[0].credits_usd,'1');assert.equal(billing.clients[0].charges_usd,'0.139128');
    assert.equal(billing.clients[0].balance_usd,'0.860872');
    const oss=billing.entries.filter(entry=>entry.model==='example/open_source');assert.equal(oss.length,50);
    for(const entry of oss){assert.equal(entry.supplier_cost_usd,'0');assert.equal(entry.amount_usd,'0.00001056');}
    report.accounting={supplier_cost_usd:'0.1155',customer_charges_usd:'0.139128',balance_usd:'0.860872',generation_events:110,jev_events:60,debits:170,unknown_fee_events:0};
  });
  await page.locator('#reload').click();
  await until(()=>page.locator('#simulation-progress-label').innerText(),value=>value.includes('50 / 50'),'Final progress not rendered');
  await snap('03-completed-overview');
  await page.locator('#simulation-view-tasks').click();
  await page.locator('#task-client').waitFor();
  assert.equal(await page.locator('#task-client').inputValue(),'ollama-customer');
  const taskFor=number=>final.tasks.find(task=>task.run===`${batch}-q${String(number).padStart(2,'0')}`);
  async function showQuestion(number) {
    const task=taskFor(number);assert(task);
    await page.locator('#task-search').fill(task.run);
    const row=page.locator(`[data-task-run=${JSON.stringify(task.run)}]`);
    await row.waitFor();
    const button=row.locator('[data-task-trace-toggle]');
    if(await button.getAttribute('aria-expanded')!=='true')await button.click();
    const detail=page.locator('[data-task-trace-row]:not([hidden]) [data-task-trace]');
    await detail.waitFor();
    return {task,row,button,detail};
  }
  await check('desktop traces show all six routing outcomes and each of the four pools',async()=>{
    const cases=[
      [1,['open_source'],'passed'],[11,['open_source','low'],'passed'],
      [21,['open_source','mid'],'passed'],[31,['open_source','mid','high'],'passed'],
      [41,['open_source','low','high'],'failed'],[46,['open_source','mid','high'],'unverifiable']
    ];
    for(const [number,tiers,status]of cases) {
      const {task,detail}=await showQuestion(number);
      assert.deepEqual(task.path.map(attempt=>attempt.tier),tiers);assert.equal(task.status,status);
      assert.equal(await detail.locator('[data-trace-tier]').count(),4);
      assert.equal(await detail.locator('[data-trace-attempt]').count(),tiers.length);
      assert.equal(await detail.locator('[data-trace-decision]').count(),tiers.length-1);
      assert.match(await detail.innerText(),/Ollama/);assert.match(await detail.innerText(),/分布不是答對機率/);
      if(number===31){
        assert.equal(task.trace.decisions[0].fallback,true);
        assert.equal(task.trace.decisions[0].fallback_reason,'tied_or_inconsistent_distribution');
        assert.match(await detail.innerText(),/分布相同或與回報選項矛盾/);
      }
      if(number===46)assert.equal(await detail.locator('[data-trace-tier]').filter({hasText:'公開驗收未知'}).count(),3);
      await snap(`04-q${String(number).padStart(2,'0')}-trace`);
    }
  });
  await check('expanded trace survives periodic refresh and returning from another page',async()=>{
    const {button}=await showQuestion(31);
    await page.waitForTimeout(5500);
    assert.equal(await button.getAttribute('aria-expanded'),'true');
    await page.locator('nav [data-page="ledger"]').click();
    await page.getByRole('heading',{name:'每一筆費用，都留下來。',exact:true}).waitFor();
    assert.match(await page.locator('#main').innerText(),/170 筆/);await snap('05-supplier-ledger');
    await page.locator('nav [data-page="tasks"]').click();
    assert.equal(await page.locator('[data-task-trace-toggle]').getAttribute('aria-expanded'),'true');
    await page.locator('nav [data-page="billing"]').click();
    await page.locator('[data-billing-entry]').first().waitFor();
    assert.equal(await page.locator('[data-billing-entry]').count(),171);
    assert.match(await page.locator('#billing-accounts').innerText(),/0\.139128/);
    assert.match(await page.locator('#billing-accounts').innerText(),/0\.860872/);
    await snap('06-customer-billing');
    await page.locator('nav [data-page="clients"]').click();
    await page.locator('[data-client="ollama-customer"]').click();
    assert.equal(await page.locator('[data-task-run]').count(),50);await snap('07-customer');
  });
  await check('read-only desktop inspection adds no tasks, fees or supplier requests',async()=>{
    const observed=await read('/monitor/snapshot'),accounts=await read('/monitor/billing');
    assert.equal(observed.summary.tasks,50);assert.equal(observed.ledger.rows.length,170);
    assert.equal(observed.simulation.new_model_or_jev_posts,170);assert.equal(observed.simulation.real_supplier_requests,0);
    assert.equal(accounts.entries.length,171);assert.equal(accounts.clients[0].charges_usd,'0.139128');
    assert.equal(report.responses.some(response=>response.path.startsWith('/monitor/billing/')&&response.method==='POST'),false);
    assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.blocked_requests,[]);assert.deepEqual(report.request_failures,[]);
    await fs.writeFile(path.join(output,'snapshot.json'),JSON.stringify(observed,null,2)+'\n',{mode:0o600});
    await fs.writeFile(path.join(output,'billing.json'),JSON.stringify(accounts,null,2)+'\n',{mode:0o600});
  });
  report.batch_id=batch;report.verified=true;
})().catch(error=>{
  report.error=String(error.message).replace(/Bearer\s+\S+/g,'Bearer [redacted]');
  process.exitCode=1;
}).finally(async()=>{
  if(!report.verified&&page){try{await snap('failure');}catch{}}
  if(output)await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
  if(browser)await browser.close();
  console.log(JSON.stringify({verified:report.verified,checks:report.checks.length,output,error:report.error}));
});
