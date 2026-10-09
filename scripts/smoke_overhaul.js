// Run through Playwright MCP browser_run_code_unsafe with this file as filename.
// Target is an isolated fixture workspace. All groups remain in simulation.
async page => {
  const base='http://127.0.0.1:8780',suffix=Date.now().toString(36),results=[];
  const assert=(value,message)=>{if(!value)throw Error(message);};
  const record=(name)=>results.push({name,passed:true});
  const screenshot=name=>page.screenshot({path:`C:/Users/justi/Documents/Python/Retail Bot Overhaul/artifacts/overhaul/${name}.png`,fullPage:true});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.unrouteAll({behavior:'wait'});await page.setViewportSize({width:1440,height:1000});await page.goto(base);
  await page.waitForFunction(()=>document.querySelector('#connection')?.dataset.connected==='true');
  const accounts=['Taylor '+suffix,'Morgan '+suffix];
  for(const [index,name] of accounts.entries()){
    await page.locator('[data-view=accounts]').click();await page.locator('#primary').click();
    await page.locator('#editor [name=name]').fill(name);await page.locator('#editor [name=email]').fill(`fixture-${suffix}-${index}@example.invalid`);
    await page.locator('#editor [type=submit]').click();await page.locator('#modal').waitFor({state:'hidden'});
    await page.getByRole('button',{name,exact:true}).waitFor();
  }
  record('Create accounts through simplified form');
  await page.locator('[data-view=task_groups]').click();if(await page.locator('[data-tg-back]').count())await page.locator('[data-tg-back]').click();
  await page.locator('#primary').click();await page.locator('#tg-editor').waitFor();
  const visibleFields=await page.locator('#tg-editor input:not([type=checkbox]):visible, #tg-editor select:visible').count();
  await page.locator('#tg-editor [name=name]').fill('Restock '+suffix);await page.locator('[name=product]').fill('https://www.amazon.com/dp/B012345678');
  for(const name of accounts)await page.locator('.account-pick').filter({hasText:name}).locator('input').check();
  assert(await page.locator('#tg-editor details[open]').count()===0,'Advanced form should start collapsed');
  await screenshot('after-create');await page.locator('#tg-editor [type=submit]').click();await page.locator('[data-tg-start]').waitFor();
  const groupId=await page.evaluate(()=>taskGroupId);
  record('Create multi-account group without opening preferences');
  await page.getByRole('button',{name:'Settings for '+accounts[0],exact:true}).click();
  await page.locator('[data-override-key=max_order_cents]').check();await page.locator('[data-override-value=max_order_cents]').fill('75');
  await page.getByRole('button',{name:'Save account settings',exact:true}).click();await page.locator('dialog[open]').waitFor({state:'hidden'});
  let data=await page.evaluate(async id=>(await fetch('/api/task-groups/'+id)).json(),groupId);
  assert(data.readiness.accounts[0].effective.max_order_cents===7500,'First account cap must be 75');
  assert(data.readiness.accounts[1].effective.max_order_cents===10000,'Second account must inherit 100');
  assert(Object.keys(data.plan.account_settings).length===1,'Only explicit account settings should be stored');
  record('Group-local override and independent inheritance');
  await page.getByRole('button',{name:'Settings for '+accounts[1],exact:true}).click();
  await page.locator('dialog[open] [name=enabled]').uncheck();await page.getByRole('button',{name:'Save account settings',exact:true}).click();await page.locator('dialog[open]').waitFor({state:'hidden'});
  assert((await page.locator('[data-account-row]').filter({hasText:accounts[1]}).innerText()).includes('Disabled'),'Disabled assignment must be visible');
  await page.getByRole('button',{name:'Settings for '+accounts[1],exact:true}).click();await page.locator('dialog[open] [name=enabled]').check();await page.getByRole('button',{name:'Save account settings',exact:true}).click();await page.locator('dialog[open]').waitFor({state:'hidden'});
  record('Disable and enable an assignment without removing it');
  await page.locator('[data-tg-edit]').click();await page.locator('#tg-editor details').first().locator('summary').first().click();await page.locator('#tg-editor [name=action]').selectOption('notify');await page.locator('#tg-editor [type=submit]').click();await page.locator('dialog[open]').waitFor({state:'hidden'});
  await page.locator('[data-tg-start]').click();await page.locator('[data-tg-command=pause]').waitFor();
  data=await page.evaluate(async id=>(await fetch('/api/task-groups/'+id)).json(),groupId);assert(data.run.state==='watching','Start must not accidentally also pause');
  await page.getByRole('button',{name:'Pause account',exact:true}).first().click();
  await page.getByRole('button',{name:'Resume account',exact:true}).click();
  await page.locator('[data-tg-command=pause]').click();await page.locator('[data-tg-command=resume]').click();await page.locator('[data-tg-command=stop]').click();await page.locator('[data-tg-start]').waitFor();
  record('Start, account pause/resume, group pause/resume and stop');
  await page.locator('[data-tg-start]').focus();
  const idle=await page.evaluate(async()=>{
    let mutations=0;const node=document.querySelector('[data-tg-start]'),observer=new MutationObserver(list=>mutations+=list.length);observer.observe(document.querySelector('#screen'),{subtree:true,childList:true,attributes:true,characterData:true});
    await new Promise(resolve=>setTimeout(resolve,4500));observer.disconnect();return {mutations,focus:document.activeElement===node};
  });assert(idle.mutations===0&&idle.focus,'Unchanged polling must preserve DOM and focus');record('Unchanged polling causes zero DOM mutations and preserves focus');
  await page.getByRole('tab',{name:'Accounts',exact:true}).focus();await page.keyboard.press('ArrowRight');assert(await page.getByRole('tab',{name:'Products',exact:true}).getAttribute('aria-selected')==='true','Tab arrow navigation');await page.keyboard.press('Home');
  await page.locator('[data-tg-edit]').click();await page.locator('dialog[open]').waitFor();await page.keyboard.press('Escape');await page.locator('dialog[open]').waitFor({state:'hidden'});assert(await page.locator('[data-tg-edit]').evaluate(el=>el===document.activeElement),'Dialog must restore focus');record('Keyboard tabs, Escape and focus restoration');
  // Intercept a read model only: backend tests exercise real failure isolation.
  await page.route(`**/api/task-groups/${groupId}`,async route=>{const response=await route.fetch();const body=await response.json();body.run.state='watching';body.readiness.accounts[0].ready=false;body.readiness.accounts[0].reason='Sign-in required';body.members[0].state='attention';await route.fulfill({response,json:body});});
  await page.waitForSelector('[data-tg-account]');assert((await page.locator('[data-account-row]').filter({hasText:accounts[0]}).innerText()).includes('Needs attention'),'Account failure must have contextual recovery');assert(!(await page.locator('[data-account-row]').filter({hasText:accounts[1]}).innerText()).includes('Needs attention'),'Failure must not affect another account');
  await page.unroute(`**/api/task-groups/${groupId}`);record('Account-specific failure and recovery UI with mocked read model');
  await page.evaluate(()=>loadTaskGroup(true));
  await page.locator('[data-tg-edit]').click();await page.locator('#tg-editor [name=schedule_kind]').selectOption('once');
  await page.locator('#tg-editor [name=date]').fill(new Date(Date.now()+7*86400000).toISOString().slice(0,10));
  await page.locator('#tg-editor [type=submit]').click();await page.locator('dialog[open]').waitFor({state:'hidden'});
  await page.locator('[data-tg-start]').click();await page.locator('[data-tg-command=stop]').waitFor();
  data=await page.evaluate(async id=>(await fetch('/api/task-groups/'+id)).json(),groupId);assert(data.run.state==='scheduled','Future release must wait for its schedule');
  await page.locator('[data-tg-command=stop]').click();await page.locator('[data-tg-start]').waitFor();record('Schedule a future release and cancel it');
  await page.locator('[data-tg-edit]').click();await page.locator('#tg-editor [name=schedule_kind]').selectOption('manual');
  await page.locator('#tg-editor details').first().locator('summary').first().click();await page.locator('#tg-editor [name=action]').selectOption('automatic');await page.locator('#tg-editor [name=goal_mode]').selectOption('multiple_success');await page.locator('#tg-editor [name=target_orders]').fill('2');
  await page.locator('#tg-editor [type=submit]').click();await page.locator('dialog[open]').waitFor({state:'hidden'});
  await page.locator('[data-tg-start]').click();await page.waitForFunction(()=>taskGroupData?.run?.state==='completed');
  data=await page.evaluate(async id=>(await fetch('/api/task-groups/'+id)).json(),groupId);assert(data.progress.confirmed_orders===2&&data.progress.reserved_orders===0,'Exactly two simulated orders');assert(data.readiness.accounts.every(a=>a.progress.confirmed_orders===1),'One confirmed order per independent account');record('Two independent simulated purchases with exact group limits');
  await page.locator('[data-tg-duplicate]').click();await page.waitForFunction(id=>taskGroupId!==id,groupId);const copyId=await page.evaluate(()=>taskGroupId);record('Duplicate preserves settings and creates a separate simulation objective');
  await page.locator('[data-tg-delete]').click();assert(await page.locator('dialog[open] [data-cancel]').evaluate(el=>el===document.activeElement),'Cancel should be focused for destructive action');await page.locator('dialog[open] [type=submit]').click();await page.locator('[data-tg-create],#primary:visible').first().waitFor();
  assert(!(await page.evaluate(async()=>(await (await fetch('/api/task-groups')).json()).groups)).some(g=>g.id===copyId),'Deleted group must leave workspace');record('Confirmed deletion');
  const navigation=['home','accounts','checkouts','events','tools','settings'];for(const view of navigation){await page.locator(`[data-view=${view}]`).click();assert((await page.locator('#screen').innerText()).length>10,'Empty navigation '+view);}
  await page.locator('[data-view=tools]').click();for(const target of ['profiles','proxies','input_lists','manager','mailboxes','solvers','troubleshooting']){await page.locator(`[data-context-view=${target}]`).click();assert((await page.locator('#screen').innerText()).length>10,'Missing tool '+target);await page.locator('[data-view=tools]').click();}record('All primary navigation and tools');
  await page.locator('[data-view=task_groups]').click();await page.locator(`[data-tg-open="${groupId}"]`).first().click();await page.locator('[data-tg-edit]').waitFor();await screenshot('after-workspace');
  await page.setViewportSize({width:390,height:844});await screenshot('after-mobile');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile page overflow');
  await page.locator('[data-tg-edit]').click();await screenshot('after-mobile-editor');assert(await page.locator('dialog[open]').evaluate(el=>el.scrollWidth<=el.clientWidth),'Mobile dialog overflow');await page.keyboard.press('Escape');record('390px workspace and editor without horizontal page overflow');
  await page.setViewportSize({width:1440,height:1000});await page.locator('[data-tg-back]').click();await page.route('**/api/task-groups',route=>route.fulfill({status:503,json:{detail:'Fixture outage'}}));await page.waitForFunction(()=>document.querySelector('#connection').dataset.connected==='false');await page.unroute('**/api/task-groups');await page.waitForFunction(()=>document.querySelector('#connection').dataset.connected==='true');record('Connection error and automatic recovery');
  assert(errors.length===0,'Browser errors: '+errors.join('; '));
  return {results,visibleFieldsExcludingAccounts:visibleFields,idle,consoleErrors:errors};
}
