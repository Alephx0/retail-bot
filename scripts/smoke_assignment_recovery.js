// Controlled network failures on the isolated simulation workspace only.
async page => {
  const assert=(ok,message)=>{if(!ok)throw Error(message);},results=[],errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.unrouteAll({behavior:'wait'});await page.goto('http://127.0.0.1:8783');
  await page.waitForFunction(()=>document.querySelector('#connection').dataset.connected==='true');
  await page.locator('[data-work-open]').first().click();await page.locator('[data-work-add]').first().click();
  const form=page.locator('#assignment-editor'),button=form.locator('[type=submit]');
  await page.waitForFunction(()=>!document.querySelector('#assignment-editor [type=submit]').disabled);
  let creates=0;
  await page.route('**/api/assignments/create',async route=>{creates++;await new Promise(resolve=>setTimeout(resolve,400));await route.fulfill({status:503,json:{detail:'Controlled save failure'}});});
  await page.route('**/api/assignments/preview',async route=>{await new Promise(resolve=>setTimeout(resolve,500));await route.continue();});
  await form.locator('[name=count]').fill('2');
  await form.evaluate(el=>el.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
  await page.waitForTimeout(150);assert(creates===0,'Pending preview must never create using old rows');assert(await button.isDisabled(),'Preview pending disables creation');
  await page.waitForFunction(()=>!document.querySelector('#assignment-editor [type=submit]').disabled);results.push('Pending preview rejects submission with stale rows');
  // An older response arrives after the newer request; it must not replace it.
  await page.unroute('**/api/assignments/preview');
  await page.route('**/api/assignments/preview',async route=>{const n=route.request().postDataJSON().count;await new Promise(resolve=>setTimeout(resolve,n===3?600:30));await route.continue();});
  await form.locator('[name=count]').fill('3');await form.locator('[name=count]').fill('4');await page.waitForTimeout(800);
  assert(await form.locator('.assignment-preview tbody tr').count()===4,'Newest response must win');results.push('Out-of-order responses keep latest assignment');
  await page.unroute('**/api/assignments/preview');await page.route('**/api/assignments/preview',route=>route.fulfill({status:503,json:{detail:'Controlled preview outage'}}));
  await form.locator('[name=count]').fill('5');await form.getByRole('button',{name:'Retry preview'}).waitFor();assert(await button.isDisabled(),'Failed preview blocks creation');
  await page.unroute('**/api/assignments/preview');await form.getByRole('button',{name:'Retry preview'}).click();await page.waitForFunction(()=>!document.querySelector('#assignment-editor [type=submit]').disabled);
  assert(await form.locator('.assignment-preview tbody tr').count()===5,'Retry recovers same draft');results.push('Preview network failure recovers without losing draft');
  await button.click();await form.evaluate(el=>el.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));await page.waitForTimeout(150);
  assert(await button.isDisabled(),'Global handlers must not re-enable pending creation');assert(creates===1,'Repeated submit must not issue duplicate creation');
  await form.getByRole('button',{name:'Retry preview'}).waitFor();assert(await button.isDisabled(),'Save failure requires fresh preview');results.push('Pending creation and save failure prevent duplicate submission');
  await form.getByRole('button',{name:'Retry preview'}).click();await page.waitForFunction(()=>!document.querySelector('#assignment-editor [type=submit]').disabled);
  await form.locator('summary').first().click();assert(await form.locator('[name=proxy_id]').isDisabled(),'Account proxy disables ignored task proxy');
  await form.locator('[name=use_account_proxy]').uncheck();assert(await form.locator('[name=proxy_id]').isEnabled(),'Explicit task proxy remains configurable');results.push('Connection controls show which source applies');
  await page.keyboard.press('Escape');await page.unrouteAll({behavior:'wait'});assert(!errors.length,errors.join('; '));return {results,errors,createRequests:creates,actualTasksCreated:0};
}
