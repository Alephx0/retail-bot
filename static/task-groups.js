'use strict';
let taskGroupId=null,taskGroupData=null,taskGroupTab='accounts',taskGroupLoading=false,taskGroupFetched=0;
Object.assign(titles,{saved_plans:['Task groups','One objective. Your accounts, working together.','Create group']});
const tgMoney=(value,region='US')=>new Intl.NumberFormat(undefined,{style:'currency',currency:({US:'USD',UK:'GBP',CA:'CAD'})[region]}).format((value||0)/100);
const tgLabel=value=>String(value||'ready').replaceAll('_',' ');
const tgActive=run=>run&&['scheduled','preparing','watching','paused','stopping'].includes(run.state);

async function loadTaskGroup(force=false){
  if(!taskGroupId||taskGroupLoading||(!force&&Date.now()-taskGroupFetched<1000))return;
  const id=taskGroupId;taskGroupLoading=true;
  try{const data=await api('task-groups/'+id);if(id===taskGroupId){const changed=JSON.stringify(data)!==JSON.stringify(taskGroupData);taskGroupData=data;taskGroupFetched=Date.now();if(view==='saved_plans'&&(changed||force))updateRegion($('#screen'),taskGroupView(false));}}
  catch(e){toast(e.message);}finally{taskGroupLoading=false;}
}
function tgProducts(g){return table(['Product / priority','Price / cap','Availability','Target','Reason / last check'],g.plan.products.map((t,i)=>{
  const rows=g.observations.filter(o=>o.product_id===t.product_id).sort((a,b)=>b.at.localeCompare(a.at)),o=rows[0];
  return `<tr><td><strong>${i+1}. ${esc(t.label||t.product_id)}</strong><small>${esc(t.product_id)}</small></td><td>${o?.price!=null?tgMoney(Math.round(o.price*100),g.plan.region):'—'}<small>Cap ${tgMoney(t.max_unit_cents,g.plan.region)}</small></td><td>${o?.available===true?'In stock':o?.available===false?'Out of stock':'Not verified'}<small>${rows.length} account observations</small></td><td>${g.plan.selection==='each'?t.desired_units+' units':'Shared group goal'}</td><td>${esc(o?.message||'Waiting to start')}<small>${o?esc(time(o.at)):''}</small></td></tr>`;
}));}
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
  if(!Object.keys(command.dataset).some(k=>k.startsWith('tg')))return;
  if(b.closest('#tg-editor'))return;
  e.preventDefault();e.stopImmediatePropagation();
  try{
    if(command.dataset.tgOpen){taskGroupId=command.dataset.tgOpen;taskGroupData=null;taskGroupTab='accounts';taskGroupFetched=0;render();}
    if(command.hasAttribute('data-tg-back')){taskGroupId=null;taskGroupData=null;render();}
    if(command.dataset.tgTab){taskGroupTab=command.dataset.tgTab;render();}
    if(command.hasAttribute('data-tg-delete')&&await confirmAction('Delete this task group?','Active work will stop. Purchase history and unresolved reservations are kept for recovery.')){await api('task-groups/'+taskGroupId,'DELETE');taskGroupId=null;taskGroupData=null;await refresh(true);toast('Task group deleted');}
    if(command.hasAttribute('data-tg-start')){b.disabled=true;await api('task-groups/'+taskGroupId+'/runs','POST',{idempotency_key:crypto.randomUUID()});await loadTaskGroup(true);}
    if(command.dataset.tgCommand){b.disabled=true;await api('group-runs/'+taskGroupData.run.id+'/'+command.dataset.tgCommand,'POST',{});await loadTaskGroup(true);}
    if(command.hasAttribute('data-tg-recheck')){await api('task-groups/'+taskGroupId+'/recheck','POST',{});await loadTaskGroup(true);}
    if(command.dataset.tgMember){await api('group-runs/'+taskGroupData.run.id+'/accounts/'+command.dataset.tgMember+'/'+command.dataset.tgMemberAction,'POST',{});await loadTaskGroup(true);}
    if(command.dataset.tgAccount){navigate('accounts');toast('Use Open on '+name('accounts',command.dataset.tgAccount)+' to check its session. Stop any active work first.');}
    if(command.dataset.tgAttempt){await api('group-attempts/'+command.dataset.tgAttempt+'/'+command.dataset.tgAttemptAction,'POST',{});if(command.dataset.tgAttemptAction==='open'){const a=taskGroupData.attempts.find(a=>a.id===command.dataset.tgAttempt);openBrowserView('group_attempts',a.id,['waiting_user','reconciliation_required'].includes(a.state));}await loadTaskGroup(true);}
    if(command.dataset.tgDetails)showTaskGroupAttemptDetails(await api('group-attempts/'+command.dataset.tgDetails));
    if(command.dataset.tgResolve)resolveTaskGroupAttempt(command.dataset.tgResolve);
  }catch(err){toast(err.message);}finally{if(b.isConnected&&view==='saved_plans'&&taskGroupData)updateRegion($('#screen'),taskGroupView(false));else b.disabled=false;}
},true);
