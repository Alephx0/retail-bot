'use strict';
let taskGroupId=null,taskGroupData=null,taskGroupTab='accounts',taskGroupLoading=false,taskGroupFetched=0;
Object.assign(titles,{task_groups:['Task groups','One objective. Your accounts, working together.','Create group']});
const tgMoney=(value,region='US')=>new Intl.NumberFormat(undefined,{style:'currency',currency:({US:'USD',UK:'GBP',CA:'CAD'})[region]}).format((value||0)/100);
const tgLabel=value=>String(value||'ready').replaceAll('_',' ');
const tgActive=run=>run&&['scheduled','preparing','watching','paused','stopping'].includes(run.state);

async function loadTaskGroup(force=false){
  if(!taskGroupId||taskGroupLoading||(!force&&Date.now()-taskGroupFetched<1000))return;
  const id=taskGroupId;taskGroupLoading=true;
  try{const data=await api('task-groups/'+id);if(id===taskGroupId){const changed=JSON.stringify(data)!==JSON.stringify(taskGroupData);taskGroupData=data;taskGroupFetched=Date.now();if(view==='task_groups'&&(changed||force))updateRegion($('#screen'),taskGroupView(false));}}
  catch(e){toast(e.message);}finally{taskGroupLoading=false;}
}
function tgProducts(g){return table(['Product / priority','Price / cap','Availability','Target','Reason / last check'],g.plan.products.map((t,i)=>{
  const rows=g.observations.filter(o=>o.product_id===t.product_id).sort((a,b)=>b.at.localeCompare(a.at)),o=rows[0];
  return `<tr><td><strong>${i+1}. ${esc(t.label||t.product_id)}</strong><small>${esc(t.product_id)}</small></td><td>${o?.price!=null?tgMoney(Math.round(o.price*100),g.plan.region):'—'}<small>Cap ${tgMoney(t.max_unit_cents,g.plan.region)}</small></td><td>${o?.available===true?'In stock':o?.available===false?'Out of stock':'Not verified'}<small>${rows.length} account observations</small></td><td>${g.plan.selection==='each'?t.desired_units+' units':'Shared group goal'}</td><td>${esc(o?.message||'Waiting to start')}<small>${o?esc(time(o.at)):''}</small></td></tr>`;
}));}
// Controls describe supported overrides; effective values are resolved by the API.
const tgOverrideFields=[
  ['action','Action','select',['notify','review','automatic','quote']],
  ['units_per_order','Units per order','number',1,30],
  ['per_account_units','Account unit limit','number',1,1000],
  ['per_account_orders','Account order limit','number',1,1000],
  ['max_order_cents','Maximum order total (cents)','number',0,100000000],
  ['per_account_spend_cents','Account spending limit (cents)','number',0,1000000000],
  ['max_unit_cents','Unit-price cap (cents; otherwise product cap)','number',0,100000000],
  ['product_ids','Product IDs / variants (comma separated)','text'],
  ['allow_third_party','Allow third-party sellers','boolean'],
  ['allow_used','Allow used products','boolean'],
  ['max_read_errors','Read failures before attention','number',1,10],
  ['retry_delay_seconds','Initial retry delay (seconds)','number',5,600],
  ['read_timeout_seconds','Read timeout (seconds)','number',5,300],
  ['checkout_timeout_seconds','Checkout / manual review timeout (seconds)','number',30,3600]
];
function editTaskGroupAccount(accountId,plan,onSave){
  const saved=plan.account_settings?.[accountId]||{enabled:true,overrides:{}};
  const controls=tgOverrideFields.map(([key,label,type,min,max])=>{
    const value=saved.overrides?.[key];
    const inherited=key==='product_ids'?plan.products.map(p=>p.product_id).join(', '):key==='max_unit_cents'?'Product caps':plan[key];
    const money=key.endsWith('_cents'),current=money&&typeof (value??inherited)==='number'?(value??inherited)/100:value??inherited;
    label=label.replace(' (cents)','').replace('(cents; otherwise product cap)','(otherwise product cap)');
    let control;
    if(type==='boolean'||type==='select'){
      const options=type==='boolean'?['false','true']:min;
      control=`<select aria-label="${esc(label)}" data-override-value="${key}">${options.map(v=>`<option value="${v}" ${String(current)===v?'selected':''}>${esc(tgLabel(v))}</option>`).join('')}</select>`;
    }else{
      control=`<input aria-label="${esc(label)}" data-override-value="${key}" type="${type}" value="${esc(Array.isArray(current)?current.join(', '):current==='Product caps'?'':current??'')}" ${type==='number'?`min="${money?min/100:min}" max="${money?max/100:max}" step="${money?'0.01':'1'}"`:''}>`;
    }
    return `<div class="tg-override"><label class="check"><input type="checkbox" data-override-key="${key}" ${value!=null?'checked':''}>Override ${esc(label)}</label><small>Inherited: ${money&&typeof inherited==='number'?tgMoney(inherited,plan.region):esc(inherited??'default')}</small>${control}</div>`;
  });
  const visible=controls.slice(0,2).join('')+controls.slice(4,5).join('');
  const advanced=[...controls.slice(2,4),...controls.slice(5)].join('');
  const d=dialog(`<form><h2>${esc(name('accounts',accountId))}: group settings</h2><p class="help">Unchecked options inherit group defaults, including future changes. Global accounts and other groups are unaffected. Shipping and payment use the retailer account's saved defaults.</p>${check('enabled','Enable account in this group',saved.enabled!==false)}<div class="override-fields">${visible}</div><details><summary>More account preferences</summary><div class="override-fields">${advanced}</div></details><p data-error role="alert"></p><div class="modal-actions"><button type="button">Cancel</button><button type="submit" class="primary">Save account settings</button></div></form>`);
  d.classList.add('tg-editor-dialog');
  const form=d.querySelector('form');
  const sync=()=>form.querySelectorAll('[data-override-key]').forEach(el=>{const field=form.querySelector(`[data-override-value="${el.dataset.overrideKey}"]`);field.disabled=!el.checked;field.required=el.checked;});
  form.onchange=sync;sync();d.querySelector('[type=button]').onclick=()=>d.close();
  form.onsubmit=async event=>{
    event.preventDefault();const overrides={};
    form.querySelectorAll('[data-override-key]:checked').forEach(el=>{
      const key=el.dataset.overrideKey,field=form.querySelector(`[data-override-value="${key}"]`),type=tgOverrideFields.find(f=>f[0]===key)[2];
      overrides[key]=key==='product_ids'?field.value.split(',').map(v=>v.trim().toUpperCase()).filter(Boolean):type==='number'?key.endsWith('_cents')?Math.round(Number(field.value)*100):Number(field.value):type==='boolean'?field.value==='true':field.value;
    });
    if(overrides.units_per_order>(overrides.per_account_units??plan.per_account_units)&&overrides.per_account_units==null)overrides.per_account_units=overrides.units_per_order;
    try{await onSave({enabled:form.elements.enabled.checked,overrides});d.close();}
    catch(error){form.querySelector('[data-error]').textContent=error.message;}
  };
}

function tgActivity(g){return `<div class="tg-attempts">${g.attempts.length?table(['Attempt / product','Account','Outcome','Reserved / spent','Actions'],[...g.attempts].reverse().map(a=>`<tr><td>${esc(a.product_id)}<small>${esc(a.id.slice(0,8))} · ${esc(time(a.created_at))}</small></td><td>${esc(a.account_id==='simulation'?'Simulation':name('accounts',a.account_id))}</td><td>${badge(a.state)} ${a.simulation?badge('simulation'):badge('live')}<small>${esc(a.message)}</small></td><td>${tgMoney(a.money_cents,a.region||g.plan.region)} · ${a.units} unit(s)</td><td><button data-tg-details="${a.id}">Details</button>${!a.simulation&&['waiting_user','reviewing','carting','preparing','reconciliation_required'].includes(a.state)?`<button data-tg-attempt="${a.id}" data-tg-attempt-action="open">View browser</button>`:''}${a.state==='waiting_user'?`<button data-tg-attempt="${a.id}" data-tg-attempt-action="check-outcome">Check outcome</button>`:''}${a.state==='reconciliation_required'?`<button data-tg-resolve="${a.id}">Resolve outcome</button>`:''}</td></tr>`)):'<p class="help">Attempts appear when matching stock is assigned to an account.</p>'}</div><div class="tg-timeline">${g.events.map(e=>`<p><time>${esc(time(e.at))}</time><strong>${esc(tgLabel(e.kind))}</strong> ${esc(e.message)}</p>`).join('')}</div>`;}

function showTaskGroupAttemptDetails(detail){
  const a=detail.attempt,p=detail.plan,b=detail.browser_profile;
  const rows=[['Product',a.product_id],['Account',a.account_id==='simulation'?'Simulation':name('accounts',a.account_id)],['Outcome',tgLabel(a.state)],['Quantity',a.units+' units'],['Reserved / spent',tgMoney(a.money_cents,a.region||p.region)],['Order number',a.order_id||'Not confirmed'],['Started',time(a.created_at)],['Action',tgLabel(p.action)+(a.simulation?' (simulation)':'')],['Order limit',tgMoney(p.max_order_cents,p.region)],['Group spending limit',tgMoney(p.max_spend_cents,p.region)],['Browser',a.simulation?'No browser used':b.fingerprint_backend==='native'?'Native Chromium':b.browser_identity||b.browser_channel||'Account default']];
  const d=dialog('<h2>Attempt details</h2><p class="help">'+esc(a.message)+'</p>'+table(['Field','Recorded value'],rows.map(([label,value])=>'<tr><td>'+esc(label)+'</td><td>'+esc(value)+'</td></tr>'))+'<details><summary>Technical record</summary><pre class="tg-detail-json">'+esc(JSON.stringify(detail,null,2))+'</pre></details><button type="button">Close</button>');
  d.classList.add('tg-attempt-dialog');d.querySelector('button').onclick=()=>d.close();
}

function resolveTaskGroupAttempt(id){
  const attempt=taskGroupData.attempts.find(a=>a.id===id);
  const d=dialog(`<form><h2>Reconcile order outcome</h2><p class="help">Check the retailer's order history and cart before recording an outcome. An uncertain result must remain reserved.</p>${select('outcome','Verified outcome',[['confirmed','An order was placed'],['no_order','No order was placed; cart checked']],'confirmed')}${input('order_id','Retailer order number')}${input('product_id','Verified product ID',attempt.product_id)}${input('units','Verified units purchased',attempt.units,'number','min="1" max="1000"')}${input('total','Verified final order total','','number','min="0" step="0.01"')}<label>Verification notes<textarea name="evidence" required minlength="12" maxlength="2000"></textarea></label><p data-error role="alert"></p><div class="modal-actions"><button type="button">Cancel</button><button type="submit" class="primary">Record verified outcome</button></div></form>`);d.querySelector('[type=button]').onclick=()=>d.close();d.querySelector('form').onsubmit=async e=>{e.preventDefault();try{const data=formData(e.target);data.units=Number(data.units);data.total_cents=data.total===''?null:Math.round(Number(data.total)*100);delete data.total;await api('group-attempts/'+id+'/resolve','POST',data);d.close();await loadTaskGroup(true);}catch(err){d.querySelector('[data-error]').textContent=err.message;}};
}
document.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;const command=b.cloneNode(false);
  if(b.id==='primary'&&view==='task_groups'){e.preventDefault();e.stopImmediatePropagation();try{if(!taskGroupId)await editTaskGroup();else if(!tgActive(taskGroupData?.run))await editTaskGroup(taskGroupData);else toast('Stop the run before editing its plan');}catch(error){toast(error.message);}return;}
  if(!Object.keys(command.dataset).some(k=>k.startsWith('tg')))return;
  if(b.closest('#tg-editor'))return;
  e.preventDefault();e.stopImmediatePropagation();
  try{
    if(command.hasAttribute('data-tg-create'))await editTaskGroup();
    if(command.dataset.tgOpen){taskGroupId=command.dataset.tgOpen;taskGroupData=null;taskGroupTab='accounts';taskGroupFetched=0;render();}
    if(command.hasAttribute('data-tg-back')){taskGroupId=null;taskGroupData=null;render();}
    if(command.dataset.tgTab){taskGroupTab=command.dataset.tgTab;render();}
    if(command.hasAttribute('data-tg-delete')&&await confirmAction('Delete this task group?','Active work will stop. Purchase history and unresolved reservations are kept for recovery.')){await api('task-groups/'+taskGroupId,'DELETE');taskGroupId=null;taskGroupData=null;await refresh(true);toast('Task group deleted');}
    if(command.hasAttribute('data-tg-edit'))await editTaskGroup(taskGroupData);
    if(command.hasAttribute('data-tg-start')){b.disabled=true;await api('task-groups/'+taskGroupId+'/runs','POST',{idempotency_key:crypto.randomUUID()});await loadTaskGroup(true);}
    if(command.dataset.tgCommand){b.disabled=true;await api('group-runs/'+taskGroupData.run.id+'/'+command.dataset.tgCommand,'POST',{});await loadTaskGroup(true);}
    if(command.hasAttribute('data-tg-recheck')){await api('task-groups/'+taskGroupId+'/recheck','POST',{});await loadTaskGroup(true);}
    if(command.hasAttribute('data-tg-duplicate')){const copy=await api('task-groups/'+taskGroupId+'/duplicate','POST',{});taskGroupId=copy.id;taskGroupData=null;await refresh(true);await loadTaskGroup(true);}
    if(command.dataset.tgSettings)editTaskGroupAccount(command.dataset.tgSettings,taskGroupData.plan,async value=>{const plan=structuredClone(taskGroupData.plan);plan.account_settings={...plan.account_settings,[command.dataset.tgSettings]:value};await api('task-groups/'+taskGroupId,'PATCH',{revision_id:taskGroupData.revision_id,plan});await loadTaskGroup(true);});
    if(command.dataset.tgMember){await api('group-runs/'+taskGroupData.run.id+'/accounts/'+command.dataset.tgMember+'/'+command.dataset.tgMemberAction,'POST',{});await loadTaskGroup(true);}
    if(command.dataset.tgAccount){navigate('accounts');toast('Use Open on '+name('accounts',command.dataset.tgAccount)+' to check its session. Stop any active work first.');}
    if(command.dataset.tgAttempt){await api('group-attempts/'+command.dataset.tgAttempt+'/'+command.dataset.tgAttemptAction,'POST',{});if(command.dataset.tgAttemptAction==='open'){const a=taskGroupData.attempts.find(a=>a.id===command.dataset.tgAttempt);openBrowserView('group_attempts',a.id,['waiting_user','reconciliation_required'].includes(a.state));}await loadTaskGroup(true);}
    if(command.dataset.tgDetails)showTaskGroupAttemptDetails(await api('group-attempts/'+command.dataset.tgDetails));
    if(command.dataset.tgResolve)resolveTaskGroupAttempt(command.dataset.tgResolve);
    if(command.dataset.tgMigrate){const preview=await api('task-group-migrations/'+command.dataset.tgMigrate+'/preview','POST',{});await editTaskGroup({plan:preview.plan},command.dataset.tgMigrate);}
  }catch(err){toast(err.message);}finally{if(b.isConnected&&view==='task_groups'&&taskGroupData)updateRegion($('#screen'),taskGroupView(false));else b.disabled=false;}
},true);
