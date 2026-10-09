'use strict';
let taskStatusFilter='all';
const taskBehavior={review:'Browser review',automatic:'Automatic checkout',monitor:'Monitor only',quote:'Checkout total'};
function taskWorkspace(){
  $('#primary').hidden=!!groupId;
  if(!groupId)return `<section class="panel">${state.groups.length?table(['Group','Retailer / product','Tasks',''],state.groups.map(g=>{
    const tasks=state.tasks.filter(t=>t.group_id===g.id),running=tasks.filter(t=>state.active.includes(t.id)).length;
    return `<tr><td><button class="item-link" data-work-open="${g.id}">${esc(g.name)}</button></td><td>${esc(retailerName(g.retailer))}<small>${esc(compactProduct(g.products||state.input_lists.find(x=>x.id===g.input_list_id)?.products))}</small></td><td>${tasks.length} tasks<small>${running} running</small></td><td>${iconButton('chevron-right','Open '+g.name,`data-work-open="${g.id}"`)}</td></tr>`;
  })):empty('layers','Create your first task group','Choose a retailer and products, then add your account tasks.','<button class="primary" data-work-create>Create group</button>')}</section>`;
  const g=state.groups.find(x=>x.id===groupId);if(!g){groupId=null;return taskWorkspace();}
  const tasks=state.tasks.filter(t=>t.group_id===g.id).sort((a,b)=>(a.created_at||a.id).localeCompare(b.created_at||b.id)),active=tasks.some(t=>state.active.includes(t.id));
  const rows=tasks.filter(t=>taskStatusFilter==='all'||taskStatusFilter==='running'&&state.active.includes(t.id)||taskStatusFilter==='attention'&&['error','attention','review'].includes(t.status)||taskStatusFilter==='completed'&&t.status==='completed');
  for(const id of selected)if(!tasks.some(t=>t.id===id))selected.delete(id);
  return `<button class="back" data-work-back>${icon('arrow-left',16)}All groups</button><div class="tg-header"><div><h1>${esc(g.name)}</h1><p>${esc(retailerName(g.retailer))} · ${tasks.length} tasks</p></div><div class="actions">${iconButton('copy','Duplicate group','data-work-duplicate')}${iconButton('trash-2','Delete group','data-work-delete')}</div></div>
    <div class="task-layout"><aside class="task-sidebar"><h2>Task group information</h2>${groupSettings(g,active)}<button data-work-schedule ${active?'disabled':''}>${icon('clock',16)}${g.schedule?.slots?.length?'Edit schedule':'Schedule'}</button><p class="help">${active?'Stop tasks to edit group settings.':g.schedule?.slots?.length?g.schedule.slots.length+' scheduled windows':'Starts when you press Start.'}</p>${g.task_group_id?'<p class="notice">This group was migrated to a purchasing plan. Its original tasks are retained as history.</p>':''}</aside>
    <section class="panel task-table"><div class="toolbar"><h2>Tasks</h2><span class="spacer"></span><button data-work-bulk="start" ${tasks.length?'':'disabled'}>${icon('play',16)}Start ${selected.size?'selected':'all'}</button><button data-work-bulk="stop" ${tasks.length?'':'disabled'}>${icon('square',16)}Stop ${selected.size?'selected':'all'}</button><button class="primary" data-work-add>${icon('plus',16)}Add tasks</button>${selected.size?`<button data-work-bulk="delete" class="danger">Delete ${selected.size}</button>`:''}</div>
    <div class="filters">${[['all','All'],['running','Running'],['attention','Needs attention'],['completed','Successful']].map(([key,label])=>`<button data-work-filter="${key}" aria-pressed="${taskStatusFilter===key}" class="${taskStatusFilter===key?'selected':''}">${label}</button>`).join('')}</div>
    ${rows.length?table(['<input type="checkbox" data-work-select-all aria-label="Select visible tasks" '+(rows.every(t=>selected.has(t.id))?'checked':'')+'>','Account','Qty','Status','Actions'],rows.map(t=>{
      const running=state.active.includes(t.id),review=['review','attention'].includes(t.status);
      return `<tr data-task-row="${t.id}"><td><input type="checkbox" data-work-select="${t.id}" aria-label="Select task for ${esc(t.account_id?name('accounts',t.account_id):'Virtual account')}" ${selected.has(t.id)?'checked':''}></td><td><strong>${esc(t.account_id?name('accounts',t.account_id):'Virtual account')}</strong><small>${t.simulation?'Simulation':'Live'} · ${esc(taskBehavior[t.checkout_mode])}</small>${t.profile_id?`<small>Profile: ${esc(name('profiles',t.profile_id))}</small>`:''}<small>${t.max_total==null?'Group limit':`Task limit: ${Number(t.max_total).toFixed(2)}`}</small></td><td>${t.quantity}</td><td>${badge(t.status||'ready')}<small>${esc(t.message||'Ready to start')}</small></td><td><div class="actions">${running?review?`<button data-work-task="${t.id}" data-work-command="resume">Resume</button>`:iconButton('square','Stop task',`data-work-task="${t.id}" data-work-command="stop"`):iconButton('play','Start task',`data-work-task="${t.id}" data-work-command="start"`)}${iconButton('ellipsis','Task details',`data-work-details="${t.id}"`)}</div></td></tr>`;
    })):empty('layers',tasks.length?'No matching tasks':'No tasks yet',tasks.length?'Choose another status filter.':'Add accounts to create independent tasks in this group.',tasks.length?'':'<button data-work-add>Add tasks</button>')}</section></div>`;
}
function taskDetails(t){
  const active=state.active.includes(t.id);
  const d=dialog(`<h2>Task details</h2><p>${esc(t.account_id?name('accounts',t.account_id):'Virtual account')} · ${t.simulation?'Simulation':'Live'}</p><p>${badge(t.status)} ${esc(t.message||'')}</p><p class="help">Quantity ${t.quantity} · ${esc(taskBehavior[t.checkout_mode])} · ${t.max_total==null?'Uses group order limit':'Order limit '+t.max_total}</p><div class="actions">${active&&!t.simulation?'<button data-task-view>View browser</button>':''}<button data-task-edit ${active?'disabled':''}>Edit task</button><button data-task-copy>Duplicate task</button><button data-task-remove class="danger">Delete task</button></div><div class="modal-actions"><button data-task-close>Close</button></div>`);
  d.querySelector('[data-task-close]').onclick=()=>d.close();
  d.querySelector('[data-task-edit]').onclick=()=>{d.close();editAccountTask(t);};
  d.querySelector('[data-task-view]')?.addEventListener('click',()=>{d.close();openBrowserView('tasks',t.id,true);});
  d.querySelector('[data-task-copy]').onclick=async()=>{try{await api('tasks','POST',{...t,simulation:true,scheduled_at:null});d.close();await refresh(true);toast('Task duplicated in simulation');}catch(e){toast(e.message);}};
  d.querySelector('[data-task-remove]').onclick=async()=>{if(await confirmAction('Delete task?','The task will stop. Order and submission records are preserved.')){try{await api('tasks/'+t.id,'DELETE');d.close();await refresh(true);}catch(e){toast(e.message);}}};
}
async function taskBulk(op,ids){
  // Cap command requests; execution itself is bounded by the engine's worker limit.
  const failures=[];
  for(let i=0;i<ids.length;i+=10){const results=await Promise.allSettled(ids.slice(i,i+10).map(id=>op==='delete'?api('tasks/'+id,'DELETE'):api('tasks/'+id+'/'+op,'POST')));for(const r of results)if(r.status==='rejected')failures.push(r.reason.message);}
  await refresh(true);if(failures.length)toast(`${failures.length} task(s) need attention: ${[...new Set(failures)].join(' ')}`);
  return failures;
}
document.addEventListener('invalid',e=>{
  const form=e.target.closest('[data-work-settings]'),panel=e.target.closest('[data-pane]');if(!form||!panel)return;
  const tab=document.querySelector('[data-work-tab="'+panel.dataset.pane+'"]');tab?.click();
},true);
document.addEventListener('submit',async e=>{
  const f=e.target;if(!f.matches('[data-work-settings]'))return;e.preventDefault();const b=f.querySelector('[type=submit]');b.disabled=true;
  try{const data=formData(f);for(const key of ['delay_ms','min_price','quantity','max_total','max_errors','retry_delay_ms','max_checkouts','min_discount','min_savings','monitor_concurrency'])data[key]=Number(data[key]);data.max_price=data.max_price===''?null:Number(data.max_price);await api('groups/'+groupId,'PUT',data);f.removeAttribute('data-preserve-draft');await refresh(true);toast('Group settings saved');}catch(error){f.querySelector('[data-work-error]').textContent=error.message;}finally{b.disabled=false;}
});
document.addEventListener('change',e=>{
  const x=e.target;if(x.hasAttribute('data-work-select')){x.checked?selected.add(x.dataset.workSelect):selected.delete(x.dataset.workSelect);render();}
  if(x.hasAttribute('data-work-select-all')){document.querySelectorAll('[data-work-select]').forEach(el=>x.checked?selected.add(el.dataset.workSelect):selected.delete(el.dataset.workSelect));render();}
});
document.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b||!Object.keys(b.dataset).some(k=>k.startsWith('work')))return;
  e.preventDefault();e.stopImmediatePropagation();const data={...b.dataset},g=state.groups.find(x=>x.id===groupId);b.disabled=true;
  try{
    if('workOpen' in data){groupId=data.workOpen;taskGroupSettingsTab='general';selected.clear();taskStatusFilter='all';render();}
    if('workBack' in data){groupId=null;selected.clear();render();}
    if('workCreate' in data)editTaskGroup();
    if('workAdd' in data)editAccountTask();
    if(data.workTab){taskGroupSettingsTab=data.workTab;const sidebar=b.closest('.task-sidebar');setPane(sidebar,taskGroupSettingsTab);sidebar.querySelectorAll('[data-work-tab]').forEach(x=>{x.classList.toggle('selected',x===b);x.setAttribute('aria-selected',String(x===b));});hydrateTabs();}
    if('workSchedule' in data)editGroupSchedule(g);
    if('workFilter' in data){taskStatusFilter=data.workFilter;render();}
    if(data.workDetails)taskDetails(state.tasks.find(t=>t.id===data.workDetails));
    if(data.workTask){await api('tasks/'+data.workTask+'/'+data.workCommand,'POST');await refresh(true);}
    if(data.workBulk){const ids=state.tasks.filter(t=>t.group_id===groupId&&(!selected.size||selected.has(t.id))).map(t=>t.id);if(data.workBulk!=='delete'||await confirmAction('Delete selected tasks?','Selected tasks will stop. Order history is retained.'))await taskBulk(data.workBulk,ids);}
    if('workDuplicate' in data){const copy=await api('task-workspace/'+groupId+'/duplicate','POST');groupId=copy.id;selected.clear();await refresh(true);toast('Group duplicated in simulation; schedules cleared');}
    if('workDelete' in data&&await confirmAction('Delete group and its tasks?','All tasks in this group will stop. Recorded orders and submission records are preserved.')){const ids=state.tasks.filter(t=>t.group_id===groupId).map(t=>t.id);if(!(await taskBulk('delete',ids)).length){await api('groups/'+groupId,'DELETE');groupId=null;selected.clear();await refresh(true);}}
  }catch(error){toast(error.message);}finally{if(b.isConnected)b.disabled=false;}
},true);
