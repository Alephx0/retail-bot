'use strict';
// Groups own monitor input. Tasks own account-specific purchase choices.
function taskForm(title,html,save,label='Save'){
  const d=dialog(`<form><h2>${esc(title)}</h2>${html}<p data-error role="alert"></p><div class="modal-actions"><button type="button">Cancel</button><button type="submit" class="primary">${label}</button></div></form>`);
  d.querySelector('[type=button]').onclick=()=>d.close();
  d.querySelector('form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('[type=submit]');b.disabled=true;try{await save(formData(e.target),e.target);d.close();await refresh(true);}catch(error){d.querySelector('[data-error]').textContent=error.message;}finally{b.disabled=false;}};
  return d;
}
function editTaskGroup(g=null){
  const current=g||{name:'',retailer:'amazon',products:'',max_total:100};
  taskForm(g?'Edit group':'Create group',
    input('name','Group name',current.name,'text','required maxlength="100"')+
    select('retailer','Retailer',retailerOptions(),current.retailer)+
    `<label for="group-products">Monitor input</label><textarea id="group-products" name="products" rows="3" placeholder="Product link or ASIN, one per line">${esc(current.products)}</textarea>`+
    input('max_total','Maximum order total',current.max_total,'number','min="0" step="0.01" required')+
    '<p class="help">Includes shipping and tax. Add account tasks after creating the group.</p>',async data=>{
      const result=await api('groups'+(g?'/'+g.id:''),g?'PUT':'POST',{...current,...data,max_total:Number(data.max_total)});groupId=result.id;taskGroupSettingsTab='general';selected.clear();
    },g?'Save group':'Create group');
}
function editAccountTask(task=null){
  if(!task){createProfileTasks();return;}
  const g=state.groups.find(x=>x.id===groupId),accounts=state.accounts.filter(a=>a.retailer===g.retailer);
  const t=task;
  const accountInput=select('account_id','Account',[['','Virtual account (simulation)'],...accounts.map(a=>[a.id,a.name])],t.account_id);
  const d=taskForm('Edit task',accountInput+
    select('profile_id','Checkout profile',[['','None'],...state.profiles.map(p=>[p.id,p.name])],t.profile_id||'')+
    '<p class="help">Amazon uses shipping and payment saved on the account. Profile assignment does not replace those defaults.</p>'+
    select('simulation','Execution',[['true','Simulation'],['false','Live']],String(t.simulation))+
    input('quantity','Item quantity',t.quantity,'number','min="1" max="30" required')+
    select('checkout_mode','Task behavior',[['review','Cart + browser review'],['automatic','Automatic checkout'],['monitor','Monitor only'],['quote','Checkout total only']],t.checkout_mode)+
    `<details><summary>Task options</summary>${input('max_total','Maximum order total',t.max_total??'','number',`min="0" step="0.01" placeholder="Use group limit: ${g.max_total}"`)}<p class="help">Leave blank to use the group limit. Each task can confirm one order; multiple tasks can produce multiple purchases.</p>${check('use_account_proxy','Use account connection',t.use_account_proxy)}${select('proxy_id','Task connection',[['','Direct'],...state.proxies.map(p=>[p.id,p.name])],t.proxy_id||'')}${check('use_buy_now','Use Buy Now when available',t.use_buy_now)}${check('force_free_shipping','Require free shipping',t.force_free_shipping)}${check('auto_open_3ds','Open browser for payment verification',t.auto_open_3ds)}${input('scheduled_at','Individual start time',t.scheduled_at?new Date(new Date(t.scheduled_at)-new Date(t.scheduled_at).getTimezoneOffset()*60000).toISOString().slice(0,16):'','datetime-local')}${input('retry_delay_ms','Retry delay (ms)',t.retry_delay_ms??3500,'number','min="1000" max="3600000" required')}</details>`,async (data,form)=>{
      const payload={...t,...data,group_id:g.id,simulation:data.simulation==='true',quantity:Number(data.quantity),max_total:data.max_total===''?null:Number(data.max_total),retry_delay_ms:Number(data.retry_delay_ms),scheduled_at:data.scheduled_at?new Date(data.scheduled_at).toISOString():null};
      await api('tasks/'+task.id,'PUT',payload);
    },'Save task');
  const f=d.querySelector('form'),sync=()=>f.elements.proxy_id.disabled=f.elements.use_account_proxy.checked;
  f.elements.use_account_proxy.onchange=sync;sync();
}
function editGroupSchedule(g){
  const s=g.schedule||{days:[],slots:[]};
  taskForm('Group schedule',`<p class="help">Times use this device's timezone (${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}). Tasks start and stop within these windows.</p><div class="weekdays">${['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((day,i)=>`<label><input type="checkbox" data-schedule-day="${i}" ${s.days?.includes(i)?'checked':''}>${day}</label>`).join('')}</div><label for="schedule-slots">Time windows</label><textarea id="schedule-slots" name="slots" placeholder="10:00 - 10:30, one window per line">${esc((s.slots||[]).map(x=>x.start+' - '+x.stop).join('\n'))}</textarea><p class="help">No weekdays means the next occurrence only. Leave windows empty to start manually. Existing tasks retain their own account and checkout settings.</p>`,async(data,form)=>{
    const slots=data.slots.split('\n').map(x=>x.trim()).filter(Boolean).map(line=>{const match=line.match(/^(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})$/);if(!match)throw Error('Use HH:MM - HH:MM for each window.');return {start:match[1],stop:match[2]};});
    const days=[...form.querySelectorAll('[data-schedule-day]:checked')].map(x=>Number(x.dataset.scheduleDay));
    // No selected weekdays means the next occurrence only, matching saved main schedules.
    await api('groups/'+g.id,'PUT',{schedule:{...s,days,slots,auto_start:false}});
  });
}
