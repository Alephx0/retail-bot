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
      const result=await api('groups'+(g?'/'+g.id:''),g?'PUT':'POST',{...current,...data,max_total:Number(data.max_total)});groupId=result.id;selected.clear();
    },g?'Save group':'Create group');
}
function editMonitorSettings(g){
  taskForm('Monitor settings',
    select('input_list_id','Saved product list',[['','Use group monitor input'],...state.input_lists.filter(x=>x.retailer===g.retailer).map(x=>[x.id,x.name])],g.input_list_id||'')+
    input('delay_ms','Check interval (ms)',g.delay_ms??4500,'number','min="3500" max="3600000" required')+
    select('mode','Monitor for',[['restock','Restocks'],['deals','Deals']],g.mode||'restock')+
    check('allow_third_party','Allow third-party sellers',g.allow_third_party)+check('allow_used','Allow used products',g.allow_used)+
    `<details><summary>Offer and deal filters</summary>${input('offer_id','Offer ID',g.offer_id||'')}${input('max_price','Maximum item price',g.max_price??'','number','min="0" step="0.01"')}${input('min_discount','Minimum discount (%)',g.min_discount||0,'number','min="0" max="100"')}${input('min_savings','Minimum savings',g.min_savings||0,'number','min="0" step="0.01"')}${check('only_freebies','Only free items in deals mode',g.only_freebies)}</details>`,async data=>{
      for(const k of ['delay_ms','min_discount','min_savings'])data[k]=Number(data[k]);data.max_price=data.max_price===''?null:Number(data.max_price);await api('groups/'+g.id,'PUT',data);
    });
}
function editAccountTask(task=null){
  const g=state.groups.find(x=>x.id===groupId),accounts=state.accounts.filter(a=>a.retailer===g.retailer);
  const t=task||{simulation:true,quantity:1,checkout_mode:'review',use_account_proxy:true};
  const accountInput=task?select('account_id','Account',[['','Virtual account (simulation)'],...accounts.map(a=>[a.id,a.name])],t.account_id):
    `<fieldset><legend>Accounts</legend><div class="account-picks">${accounts.map(a=>`<label class="account-pick"><input type="checkbox" data-task-account="${a.id}"><span>${esc(a.name)}<small>${esc(a.email||'')}</small></span></label>`).join('')||'<p class="help">Add an account in Accounts, or test with a virtual account.</p>'}</div><p class="help">One independent task per selected account. No selection uses a virtual account in simulation.</p></fieldset>`;
  const d=taskForm(task?'Edit task':'Add tasks',accountInput+
    select('simulation','Execution',[['true','Simulation'],['false','Live']],String(t.simulation))+
    input('quantity','Item quantity',t.quantity,'number','min="1" max="30" required')+
    select('checkout_mode','Task behavior',[['review','Cart + browser review'],['automatic','Automatic checkout'],['monitor','Monitor only'],['quote','Checkout total only']],t.checkout_mode)+
    `<details><summary>Task options</summary>${input('max_total','Maximum order total',t.max_total??'','number',`min="0" step="0.01" placeholder="Use group limit: ${g.max_total}"`)}<p class="help">Leave blank to use the group limit. Each task can confirm one order; multiple tasks can produce multiple purchases.</p>${check('use_account_proxy','Use account connection',t.use_account_proxy)}${select('proxy_id','Task connection',[['','Direct'],...state.proxies.map(p=>[p.id,p.name])],t.proxy_id||'')}${check('use_buy_now','Use Buy Now when available',t.use_buy_now)}${check('force_free_shipping','Require free shipping',t.force_free_shipping)}${check('auto_open_3ds','Open browser for payment verification',t.auto_open_3ds)}${input('scheduled_at','Individual start time',t.scheduled_at?new Date(new Date(t.scheduled_at)-new Date(t.scheduled_at).getTimezoneOffset()*60000).toISOString().slice(0,16):'','datetime-local')}${input('retry_delay_ms','Retry delay (ms)',t.retry_delay_ms??3500,'number','min="1000" max="3600000" required')}${task?'':input('task_count','Tasks per account',1,'number','min="1" max="100" required')}</details>`,async (data,form)=>{
      const payload={...t,...data,group_id:g.id,simulation:data.simulation==='true',quantity:Number(data.quantity),max_total:data.max_total===''?null:Number(data.max_total),retry_delay_ms:Number(data.retry_delay_ms),scheduled_at:data.scheduled_at?new Date(data.scheduled_at).toISOString():null};
      if(task){await api('tasks/'+task.id,'PUT',payload);return;}
      const ids=[...form.querySelectorAll('[data-task-account]:checked')].map(el=>el.dataset.taskAccount);
      if(!ids.length){if(!payload.simulation)throw Error('Select an account for a live task.');if(Number(data.task_count)!==1)throw Error('Select accounts to create multiple tasks.');await api('tasks','POST',payload);}
      else await api('task-batches/create','POST',{...payload,account_ids:ids,task_count:Number(data.task_count)});
    },task?'Save task':'Create tasks');
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
