/* Actual desktop control only; fixture credentials stay in process memory. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const readline = require('node:readline');
const { chromium } = require(process.env.CLOUDYFAN_PLAYWRIGHT_MODULE ||
  '/Users/alexwang0315/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const origin = process.argv[2], output = process.argv[3];
const incoming = readline.createInterface({input:process.stdin})[Symbol.asyncIterator]();
const report = {schema_version:1, viewport:{width:1280,height:800},
  verified:false, checks:[], http_responses:[], blocked_requests:[], page_errors:[]};
let browser;

async function snap(page, name) {
  await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
  await fs.writeFile(path.join(output,name+'.aria.txt'),await page.locator('body').ariaSnapshot());
}
async function check(name, action) {
  await action();
  report.checks.push({name,passed:true});
}

(async()=>{
  const fixture=JSON.parse((await incoming.next()).value);
  browser=await chromium.launch({channel:'chrome',headless:true});
  report.browser=browser.version();
  const context=await browser.newContext({viewport:report.viewport,locale:'zh-TW'});
  await context.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin){report.blocked_requests.push(url.origin+url.pathname);await route.abort();return;}
    await route.continue();
  });
  const page=await context.newPage();
  page.on('pageerror',()=>report.page_errors.push('JavaScript page error'));
  page.on('response',response=>{
    const url=new URL(response.url());
    report.http_responses.push({method:response.request().method(),path:url.pathname,status:response.status()});
  });
  await page.goto(origin);
  await page.getByRole('link',{name:/連線客戶端/}).click();
  await page.locator('.client-card').filter({hasText:'studio'}).getByRole('button',{name:/查看此客戶端/}).click();
  await page.getByRole('row').filter({hasText:'identity-desktop-studio'}).waitFor();
  await check('synthetic environment and original identity history are visible',async()=>{
    assert.match(await page.locator('#environment').innerText(),/模擬/);
    assert.equal(await page.locator('.client-card').count(),3);
    const studio=page.locator('.client-card').filter({hasText:'studio'});
    assert.match(await studio.locator('.client-numbers').innerText(),/1\s*任務/);
    assert.match(await page.getByRole('row').filter({hasText:'identity-desktop-studio'}).innerText(),/通過/);
    await snap(page,'clients-before-rotation');
  });
  const beforeCard=await page.locator('.client-card').filter({hasText:'studio'}).locator('.client-numbers').innerText();
  await page.locator('#logout').click();
  await page.locator('#token').fill(fixture.client_token);
  await page.getByRole('button',{name:'登入',exact:true}).click();
  await check('client credential cannot log into operator console',async()=>{
    await page.locator('#login-error').filter({hasText:'登入憑證不正確或已失效'}).waitFor();
    assert.equal(await page.locator('#login').isVisible(),true);
    // Password input is cleared before either screenshot or accessibility capture.
    await page.locator('#token').fill('');
    await snap(page,'client-login-rejected');
  });
  const disposableSession=await context.request.get(origin+'/demo/session');
  assert.equal(disposableSession.status(),200);
  const session=await disposableSession.json();
  assert.equal(session.demo,true);
  await page.locator('#token').fill(session.token);
  await page.getByRole('button',{name:'登入',exact:true}).click();
  await page.locator('#login').waitFor({state:'hidden'});
  await page.getByRole('row').filter({hasText:'identity-desktop-studio'}).waitFor();

  process.stdout.write(JSON.stringify({action:'rotate'})+'\n');
  const rotated=JSON.parse((await incoming.next()).value);
  assert.equal(rotated.rotated,true);
  assert.equal(rotated.mock_calls,2);
  await page.locator('#reload').click();
  await page.getByRole('row').filter({hasText:'identity-desktop-studio'}).waitFor();
  await check('rotation preserves original identity and historical statistics',async()=>{
    assert.equal(await page.locator('.client-card').count(),3);
    const studio=page.locator('.client-card').filter({hasText:'studio'});
    assert.equal(await studio.locator('.client-numbers').innerText(),beforeCard);
    assert.equal(await page.getByRole('row').filter({hasText:'identity-desktop-studio'}).count(),1);
    assert.match(await page.getByRole('row').filter({hasText:'identity-desktop-studio'}).innerText(),/通過/);
    await snap(page,'clients-after-rotation');
  });
  await page.getByRole('link',{name:/費用帳本/}).click();
  await check('attributed client cost stays visible without private payloads',async()=>{
    await page.getByRole('heading',{name:'費用歸屬',exact:true}).waitFor();
    const attribution=page.locator('section').filter({has:page.getByRole('heading',{name:'費用歸屬',exact:true})});
    assert.match(await attribution.getByRole('row').filter({hasText:'Studio 工作台'}).innerText(),/\$0\.000100/);
    assert.match(await attribution.getByRole('row').filter({hasText:'Agent \/ SDK'}).innerText(),/\$0\.000100/);
    const visible=await page.locator('body').innerText();
    assert.equal(visible.includes('Return only the integer sum of 1 and 1.'),false);
    assert.equal(visible.includes(fixture.client_token),false);
    assert.equal(visible.includes(session.token),false);
    assert.equal(await page.locator('#token').inputValue(),'');
    await snap(page,'ledger-after-rotation');
  });
  assert.deepEqual(report.page_errors,[]);
  assert.deepEqual(report.blocked_requests,[]);
  assert.equal(report.http_responses.filter(r=>r.path==='/monitor/snapshot'&&r.status===401).length,1);
  report.verified=true;
})().catch(()=>{report.error='Desktop acceptance failed; inspect safe ARIA and screenshots';process.exitCode=1;})
.finally(async()=>{
  if(browser)await browser.close();
  await fs.writeFile(path.join(output,'browser-report.json'),JSON.stringify(report,null,2));
  process.stdin.destroy();
});
