/* Desktop-only accounting acceptance against an explicitly supplied local fixture. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const readline = require('node:readline');
const {chromium} = require(process.env.CLOUDYFAN_PLAYWRIGHT_MODULE ||
  '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

let origin = process.argv[2];
const output = process.argv[3];
const incoming = readline.createInterface({input:process.stdin})[Symbol.asyncIterator]();
const report = {schema_version:1,viewport:{width:1280,height:800},verified:false,
  checks:[],http_responses:[],blocked_requests:[],request_failures:[],page_errors:[]};
let browser, page;
async function check(name, action) { await action(); report.checks.push({name,passed:true}); }
async function snap(name) {
  await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
  await fs.writeFile(path.join(output,name+'.aria.txt'),await page.locator('body').ariaSnapshot(),{mode:0o600});
}
async function applyConfirmation() {
  await page.locator('#confirm').waitFor({state:'visible'});
  await page.locator('#apply-confirm').click();
  await page.locator('#confirm').waitFor({state:'hidden'});
}
async function until(read, predicate, message) {
  const deadline=Date.now()+15000;
  while(Date.now()<deadline) {
    const value=await read();
    if(predicate(value)) return value;
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  assert.fail(message);
}

(async()=>{
  const target=new URL(origin);
  assert.equal(target.protocol,'http:');
  assert.equal(target.hostname,'127.0.0.1');
  assert.notEqual(target.port,'18767','Never run this QA against the original preview');
  origin=target.origin;
  assert(output,'Provide a private QA output directory');
  await fs.mkdir(output,{recursive:true,mode:0o700});
  const first=await incoming.next();
  assert.equal(first.done,false,'Provide the synthetic client fixture on stdin');
  const fixture=JSON.parse(first.value);
  assert.equal(typeof fixture.client_token,'string');
  const clientId=fixture.client_id || 'studio';
  const category=fixture.category || 'artificial-analysis:coding_index';
  browser=await chromium.launch({channel:'chrome',headless:true});
  report.browser=browser.version();
  const context=await browser.newContext({viewport:report.viewport,locale:'zh-TW'});
  await context.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin){report.blocked_requests.push(url.origin+url.pathname);await route.abort();return;}
    await route.continue();
  });
  page=await context.newPage();
  page.on('pageerror',()=>report.page_errors.push('JavaScript page error'));
  page.on('requestfailed',request=>{
    const url=new URL(request.url());
    report.request_failures.push({method:request.method(),path:url.pathname});
  });
  page.on('response',response=>{
    const url=new URL(response.url());
    report.http_responses.push({method:response.request().method(),path:url.pathname,status:response.status()});
  });
  const sessionResponse=await context.request.get(origin+'/demo/session');
  assert.equal(sessionResponse.status(),200);
  const session=await sessionResponse.json();
  assert.equal(session.demo,true,'Only synthetic fixtures are supported');
  const adminHeaders={Authorization:'Bearer '+session.token};
  const readBilling=async()=>{
    const response=await context.request.get(origin+'/monitor/billing',{headers:adminHeaders});
    assert.equal(response.status(),200);return response.json();
  };
  await page.goto(origin);
  await page.getByRole('link',{name:/客戶帳務/}).click();
  await page.locator('#billing-multiplier').waitFor();
  await check('accounting opens as a synthetic internal-dollar ledger without automatic posting',async()=>{
    assert.match(await page.locator('#billing-environment').innerText(),/合成帳務/);
    assert.equal(report.http_responses.some(r=>r.method==='POST'&&r.path.startsWith('/monitor/billing')),false);
    assert.match(await page.locator('#billing-source').innerText(),/金鑰範圍/);
    assert.match(await page.locator('#billing-source').innerText(),/帳戶範圍/);
    assert.match(await page.locator('#billing-source').innerText(),/未知/);
    await snap('billing-initial');
  });
  await check('unsaved pricing survives refresh, background rendering and page return',async()=>{
    await page.locator('#billing-multiplier').fill('1.35');
    await page.getByRole('heading',{name:'客戶帳務與售價',exact:true}).click();
    await page.waitForTimeout(5500);
    assert.equal(await page.locator('#billing-multiplier').inputValue(),'1.35');
    await page.locator('#billing-reload').click();
    assert.equal(await page.locator('#billing-multiplier').inputValue(),'1.35');
    await page.getByRole('link',{name:/運作總覽/}).click();
    await page.getByRole('link',{name:/客戶帳務/}).click();
    await page.locator('#billing-multiplier').waitFor();
    assert.equal(await page.locator('#billing-multiplier').inputValue(),'1.35');
  });
  await check('versioned multipliers and separate input/output open-source prices can be confirmed',async()=>{
    await page.locator('#billing-enabled').check();
    await page.locator('#billing-multiplier').fill('1.2');
    await page.locator('#billing-add-override').click();
    await page.locator('[data-override-client]').last().selectOption(clientId);
    await page.locator('[data-override-rate]').last().fill('0.9');
    await page.locator('#billing-add-price').click();
    await page.locator('[data-price-model]').last().fill('example/open_source');
    await page.locator('[data-price-input]').last().fill('0.4');
    await page.locator('[data-price-output]').last().fill('0.8');
    await page.locator('#billing-save-settings').click();
    await applyConfirmation();
    await page.locator('#toast').filter({hasText:/售價已保存/}).waitFor();
    const view=await readBilling();
    assert.equal(view.settings.enabled,true);
    assert.equal(view.settings.multiplier,'1.2');
    assert.equal(view.settings.client_multipliers[clientId],'0.9');
    assert.deepEqual(view.settings.open_source_prices['example/open_source'],{input_per_million:'0.4',output_per_million:'0.8'});
    await snap('billing-prices-saved');
  });
  const creditKeys=[];
  let firstCreditStatus=null;
  await page.route('**/monitor/billing/credits',async route=>{
    if(route.request().method()!=='POST'){await route.continue();return;}
    creditKeys.push(route.request().postDataJSON().idempotency_key);
    if(creditKeys.length===1){
      const response=await route.fetch();firstCreditStatus=response.status();
      await route.abort('failed');return;
    }
    await route.continue();
  });
  await check('a lost credit response survives full reload, blocks a missing client, and retries with one credit identity',async()=>{
    await page.locator('#billing-credit-client').selectOption(clientId);
    await page.locator('#billing-credit-amount').fill('1');
    await page.locator('#billing-credit-note').fill('Synthetic desktop acceptance credit');
    await page.locator('#billing-credit-submit').click();await applyConfirmation();
    await page.getByRole('button',{name:'重試同一筆入帳',exact:true}).waitFor();
    assert.equal(firstCreditStatus,200);
    const stored=await page.evaluate(()=>sessionStorage.getItem('cloudyfan.billing.pending-credit.v1'));
    assert.equal(JSON.parse(stored).idempotency_key,creditKeys[0]);
    assert.equal(stored.includes(session.token),false);assert.equal(stored.includes(fixture.client_token),false);
    await page.route('**/monitor/billing',async route=>{
      const response=await route.fetch();
      const value=await response.json();
      await route.fulfill({response,json:{...value,clients:value.clients.filter(client=>client.id!==clientId)}});
    });
    await page.reload();
    await page.locator('#billing-credit-recovery').filter({hasText:/已找回重新載入前/}).waitFor();
    assert.equal(await page.locator('#billing-credit-submit').isDisabled(),true);
    assert.equal(await page.locator('#billing-credit-client').inputValue(),clientId);
    assert.match(await page.locator('#billing-credit').innerText(),/原客戶目前不在清單/);
    await page.unroute('**/monitor/billing');
    await page.locator('#billing-reload').click();
    await until(()=>page.locator('#billing-credit-submit').isEnabled(),Boolean,'Original client recovery remained blocked');
    await page.getByRole('link',{name:/運作總覽/}).click();
    await page.getByRole('link',{name:/客戶帳務/}).click();
    await page.getByRole('button',{name:'重試同一筆入帳',exact:true}).waitFor();
    await page.locator('#billing-credit-submit').click();await applyConfirmation();
    await page.locator('#toast').filter({hasText:/客戶美元入帳已記錄/}).waitFor();
    assert.equal(creditKeys.length,2);assert.equal(creditKeys[0],creditKeys[1]);
    const view=await readBilling();
    assert.equal(view.entries.filter(e=>e.kind==='credit'&&e.client===clientId).length,1);
    assert.equal(await page.locator('#billing-credit-amount').inputValue(),'');
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('cloudyfan.billing.pending-credit.v1')),null);
    report.credit_response_loss={first_server_status:firstCreditStatus,full_reload_recovered:true,missing_client_blocked:true,retry_same_id:true,credit_records:1};
    await snap('billing-credit-idempotent');
  });
  const runId='billing-desktop-'+Date.now().toString(36);
  const created=await context.request.post(origin+'/v1/tasks',{
    headers:{Authorization:'Bearer '+fixture.client_token},
    data:{run_id:runId,category,prompt:'Return only the integer sum of 1 and 1.'}
  });
  assert.equal(created.status(),200);
  await check('manual reconciliation records charges once and leaves provider-account refresh explicit',async()=>{
    await page.locator('#billing-reconcile').click();await applyConfirmation();
    const first=await until(readBilling,view=>view.entries.some(e=>e.kind==='debit'&&e.run_id===runId),'No synthetic task charge was recorded');
    const ids=first.entries.filter(e=>e.kind==='debit').map(e=>e.id).sort();
    assert(ids.length>0);
    await page.locator('#billing-reconcile').click();await applyConfirmation();
    const second=await readBilling();
    assert.deepEqual(second.entries.filter(e=>e.kind==='debit').map(e=>e.id).sort(),ids);
    assert.equal(report.http_responses.some(r=>r.method==='POST'&&r.path==='/monitor/billing/source/account'),false);
    assert.equal((await page.locator('body').innerText()).includes('billing_supplier_cost_pending'),false);
    assert.equal((await page.locator('body').innerText()).includes(session.token),false);
    assert.equal((await page.locator('body').innerText()).includes(fixture.client_token),false);
    await snap('billing-after-reconciliation');
  });
  await check('client credentials cannot access operator accounting',async()=>{
    const response=await context.request.get(origin+'/monitor/billing',{headers:{Authorization:'Bearer '+fixture.client_token}});
    assert.equal(response.status(),401);
    assert.equal((await response.json()).error.code,'unauthorized');
    report.client_operator_billing_status=response.status();
  });
  await check('unavailable session storage blocks new credit and asks for ledger verification',async()=>{
    const storagePage=await context.newPage();
    storagePage.on('pageerror',()=>report.page_errors.push('JavaScript page error'));
    await storagePage.addInitScript(()=>Object.defineProperty(window,'sessionStorage',{get(){throw new DOMException('Storage disabled for acceptance','SecurityError');}}));
    await storagePage.goto(origin+'/#billing');
    await storagePage.locator('#billing-credit-storage-warning').waitFor();
    assert.equal(await storagePage.locator('#billing-credit-submit').isDisabled(),true);
    assert.match(await storagePage.locator('#billing-credit-storage-warning').innerText(),/先核對帳本/);
    assert.equal(creditKeys.length,2);
    await storagePage.screenshot({path:path.join(output,'billing-storage-unavailable.png'),fullPage:true});
    await fs.writeFile(path.join(output,'billing-storage-unavailable.aria.txt'),await storagePage.locator('body').ariaSnapshot(),{mode:0o600});
    await storagePage.close();
  });
  assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.blocked_requests,[]);
  assert.deepEqual(report.request_failures,[{method:'POST',path:'/monitor/billing/credits'}]);
  report.verified=true;
})().catch(async()=>{
  report.error='Desktop accounting acceptance failed; inspect private safe screenshots and ARIA';
  process.exitCode=1;
  if(page){
    try{
      if(await page.locator('#token').isVisible())await page.locator('#token').fill('');
      await snap('billing-failure');
    }catch{}
  }
}).finally(async()=>{
  if(browser)await browser.close();
  if(output){await fs.mkdir(output,{recursive:true,mode:0o700});await fs.writeFile(path.join(output,'billing-browser-report.json'),JSON.stringify(report,null,2),{mode:0o600});}
  process.stdin.destroy();
});
