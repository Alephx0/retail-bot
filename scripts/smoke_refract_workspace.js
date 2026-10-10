// Playwright MCP; target the isolated 8785 fixture instance only.
async page => {
  const results=[],errors=[],check=(ok,message)=>{if(!ok)throw Error(message);};
  page.on('pageerror',e=>errors.push(e.message));
  await page.unrouteAll({behavior:'wait'});
  await page.setViewportSize({width:1440,height:1000});await page.goto('http://127.0.0.1:8785');
  await page.waitForFunction(()=>stateLoaded);
  check(await page.locator('.nav').evaluateAll(nodes=>nodes.every(node=>{
    const svg=node.querySelector('svg'),box=svg.getBoundingClientRect();
    return box.width>=20&&box.height>=20&&getComputedStyle(svg.parentElement).clipPath==='none';
  })),'Every icon remains visible after hydration in the desktop rail');
  await page.evaluate(async()=>{for(const id of state.active)await api('tasks/'+id+'/stop','POST');await refresh(true);});
  const fixture=await page.evaluate(async()=>{
    const a=await api('accounts','POST',{name:'Search Alpha',email:'alpha@example.invalid'}),b=await api('accounts','POST',{name:'Search Beta',email:'beta@example.invalid'});
    const g=await api('groups','POST',{name:'Search isolation '+Date.now(),products:'B012345678\nB087654321'});
    const tasks=[];for(const account of [a,b])tasks.push(await api('tasks','POST',{group_id:g.id,account_id:account.id,simulation:true,checkout_mode:'monitor'}));
    const folder=await api('folders','POST',{name:'Search folder',resource_kind:'accounts'});await api('organization/members','POST',{folder_id:folder.id,ids:[a.id]});
    await refresh(true);return {g,a,b,tasks,folder};
  });
  await page.getByRole('searchbox',{name:'Search task groups'}).fill(fixture.g.name);
  check(await page.locator('[data-work-open]').count()===1,'Group search filters cards');
  check(await page.getByRole('searchbox').evaluate(el=>document.activeElement===el),'Search keeps focus');
  await page.locator(`[data-work-open="${fixture.g.id}"]`).click();
  await page.locator('[data-work-add]').first().click();
  check(await page.evaluate(()=>{const ids=[...document.querySelectorAll('[id]')].map(e=>e.id);return new Set(ids).size===ids.length;}),'Dialog fields have unique IDs beside group settings');
  check(await page.getByLabel('Item quantity',{exact:true}).evaluate(el=>!!el.closest('dialog[open]')),'Modal label targets its own input');
  await page.keyboard.press('Escape');
  const search=page.getByRole('searchbox',{name:'Search account tasks'});await search.fill('Alpha');
  check(await page.locator('[data-task-row]').count()===1,'Task search isolates account');
  await page.getByRole('button',{name:'Start visible',exact:true}).click();
  await page.waitForFunction(id=>state.active.includes(id),fixture.tasks[0].id);
  check(!(await page.evaluate(()=>state.active)).includes(fixture.tasks[1].id),'Filtered start does not start hidden account');
  await page.getByRole('button',{name:'Stop visible',exact:true}).click();
  await page.waitForFunction(id=>!state.active.includes(id),fixture.tasks[0].id);
  await search.fill('');
  await page.locator('[data-work-bulk=start]').click();
  await page.waitForFunction(ids=>ids.every(id=>state.active.includes(id)),fixture.tasks.map(t=>t.id));
  await page.locator('[data-work-section=monitors]').click();
  check(await page.evaluate(()=>state.monitors.filter(m=>m.group_id===groupId&&m.task_ids.length===2).length)===2,'Two shared product monitors for two accounts');
  await page.locator('[data-monitor-log]:visible').first().click();
  check((await page.locator('[data-monitor-log-body]').innerText()).includes('Simulation'),'Monitor log is live fixture data');
  await page.locator('[data-close-monitor-log]').click();
  await page.locator('[data-work-section=tasks]').click();await page.locator('[data-work-bulk=stop]').click();
  await page.waitForFunction(ids=>ids.every(id=>!state.active.includes(id)),fixture.tasks.map(t=>t.id));
  results.push('Group/task search, focus retention, visible-only execution, shared monitor tabs and logs');
  await page.locator('[data-view=accounts]').click();await page.locator(`[data-folder="${fixture.folder.id}"]`).click();
  check(await page.locator('[data-account-row]').count()===1,'Folder membership respected');
  await page.getByRole('searchbox').fill('Beta');check(await page.locator('[data-account-row]').count()===0,'Search within folder');
  check((await page.locator('#screen').innerText()).includes('No matching items'),'Search empty state');
  await page.getByRole('searchbox').fill('Alpha');await page.locator('[data-fingerprint-edit]').click();
  check((await page.locator('dialog[open]').innerText()).includes('Generated fingerprint'),'Account fingerprint editor preserved');
  await page.keyboard.press('Escape');results.push('Account folders, empty search state and fingerprint editor');
  await page.locator('[data-view=profiles]').click();await page.locator('#primary').click();
  await page.locator('#editor [name=name]').fill('UI profile');
  await page.locator('[data-profile-tab=shipping]').click();await page.locator('#editor [name=shipping_city]').fill('Boston');
  await page.locator('[data-profile-tab=payment]').click();await page.locator('#editor [name=card_name]').fill('Fixture');
  await page.locator('#editor [type=submit]').click();await page.locator('dialog[open]').waitFor({state:'hidden'});
  check(await page.evaluate(()=>state.profiles.some(p=>p.name==='UI profile'&&p.shipping.city==='Boston')),'Hidden profile tab values persist');
  results.push('Profile tabs preserve shipping and payment fields');
  await page.locator('[data-view=checkouts]').click();
  for(const key of ['monitors','events','feed','saved_plans','checkouts']){await page.locator(`#section-tabs [data-context-view=${key}]`).click();check((await page.locator('#screen').innerText()).length>0,'Operations '+key);}
  await page.locator('[data-view=tools]').click();
  for(const key of ['input_lists','mailboxes','solvers','troubleshooting','retailers','tools']){await page.locator(`#section-tabs [data-context-view=${key}]`).click();check((await page.locator('#screen').innerText()).length>0,'Tools '+key);}
  results.push('Every Operations and Tools tab reaches an existing feature');
  await page.locator('[data-view=settings]').click();await page.locator('[data-settings-tab=general]').focus();await page.keyboard.press('ArrowRight');
  check(await page.locator('[data-settings-tab=browser]').getAttribute('aria-selected')==='true','Horizontal settings keyboard navigation');
  await page.locator('[data-settings-tab=general]').click();const workerLimit=await page.locator('[name=max_running_tasks]').inputValue()==='9'?10:9;await page.locator('[name=max_running_tasks]').fill(String(workerLimit));
  await page.locator('[data-settings-tab=notifications]').click();await page.locator('#settings-form [type=submit]').click();
  await page.waitForFunction(value=>state.settings[0].max_running_tasks===value,workerLimit);results.push('Settings tabs share a draft and persist changes');
  for(const width of [1024,760,390]){
    await page.setViewportSize({width,height:844});
    for(const key of ['home','accounts','profiles','proxies','checkouts','tools','settings','task_groups']){
      await page.locator(`[data-view=${key}]`).click();
      check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`Overflow ${key} ${width}`);
    }
  }
  await page.screenshot({path:'C:/Users/justi/Documents/Python/Retail Bot/artifacts/after-groups-mobile.png',fullPage:true});
  await page.setViewportSize({width:1440,height:1000});results.push('Eight destinations fit 1024px, 760px and 390px');
  check(!errors.length,'Browser errors: '+errors.join('; '));return {results,errors};
}
