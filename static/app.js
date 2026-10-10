"use strict";
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state={groups:[],tasks:[],accounts:[],proxies:[],events:[],checkouts:[],feed:[],settings:[],active:[],profiles:[],mailboxes:[],solvers:[],input_lists:[],retailers:[],proxy_health:[],harvesters:[],task_groups:[]};
let view='task_groups',groupId=null,search='',editing=null,selected=new Set(),historyFilter='all',stateLoaded=false;
let refreshPending=false,toastTimer;
const titles={...featureTitles,task_groups:['Task groups','Your groups, accounts and independent tasks.','Create group'],home:['Overview','Your workspace at a glance.',''],checkouts:['Order history','Confirmed purchases and recorded outcomes.','Export CSV'],events:['Activity','Recent workspace activity.',''],feed:['Product observations','Availability from your running groups.',''],settings:['Settings','Defaults that work for your whole workspace.',''],tools:['Tools','Optional resources and workspace utilities.','']};
async function api(path,method='GET',body){
  const response=await fetch('/api/'+path,{method,headers:{'Content-Type':'application/json','X-Retail-Client':'dashboard'},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok){const detail=data.detail;throw new Error(typeof detail==='string'?detail:Array.isArray(detail)?detail.map(e=>(e.loc?.slice(1).join(' ')||'Setting')+': '+e.msg).join('. '):'This action could not be completed. Please try again.');}
  return data;
}
function toast(message){$('#toast').textContent=message;$('#toast').style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').style.display='none',6000);}
const statusNames={idle:'Ready',starting:'Starting',monitoring:'Running',in_queue:'Queued',review:'Needs review',checkout:'Checking out',not_needed:'Goal reached',watching:'Running',preparing:'Starting',scheduled:'Scheduled',paused:'Paused',stopped:'Stopped',stopping:'Stopping',completed:'Successful',confirmed:'Successful',reserved:'Waiting',carting:'Checking out',reviewing:'Checking out',submitting:'Confirming order',waiting_user:'Needs review',reconciliation_required:'Verify outcome',read_error:'Needs attention',attention:'Needs attention',retrying:'Retrying',failed:'Failed',error:'Failed',cancelled:'Stopped',rejected:'Not eligible',quoted:'Price verified',eligible:'Available',out_of_stock:'Waiting for stock',waiting:'Waiting',ready:'Ready',disabled:'Disabled'};
function badge(status='ready'){
  const color=['completed','confirmed','ready','saved','connected'].includes(status)?'success':['failed','error','reconciliation_required'].includes(status)?'error':['paused','attention','read_error','waiting_user','retrying'].includes(status)?'warning':['watching','running','preparing','carting','submitting'].includes(status)?'processing':'neutral';
  return `<span class="badge ${color}"><span class="status-dot"></span>${esc(statusNames[status]||String(status).replaceAll('_',' '))}</span>`;
}
function name(kind,id){return state[kind]?.find(x=>x.id===id)?.name||'Unassigned';}
function time(value){return value?new Date(value).toLocaleString():'Not yet';}
function table(headers,rows){return `<div class="table-wrap"><table><thead><tr>${headers.map(x=>`<th scope="col">${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;}
function empty(glyph,title,description,actions=''){return `<div class="empty">${icon(ICONS[glyph]?glyph:'package',28)}<h2>${title}</h2><p>${description}</p>${actions}</div>`;}
async function refresh(force=false){
  if(refreshPending||(!force&&document.hidden))return;
  refreshPending=true;
  try{
    if(view==='task_groups'&&stateLoaded&&!force){
      const next=await api('task-workspace');
      const changed=['groups','tasks','active','monitors','task_browser_ids'].some(key=>JSON.stringify(next[key])!==JSON.stringify(state[key]));
      Object.assign(state,next);if(changed)render();
    }else if(view==='saved_plans'&&stateLoaded&&!force){
      if(taskGroupId)await loadTaskGroup();
      else{const next=(await api('task-groups')).groups;if(JSON.stringify(next)!==JSON.stringify(state.task_groups)){state.task_groups=next;render();}}
    }else{
      const next=await api('state');const changed=JSON.stringify(next)!==JSON.stringify(state);
      state=next;stateLoaded=true;processSounds();if(changed||force)render();
    }
    refreshMonitorStatus();$('#connection').textContent='Engine connected';$('#connection').dataset.connected='true';
  }catch(error){$('#connection').textContent='Connection lost';$('#connection').dataset.connected='false';if(!stateLoaded)$('#screen').innerHTML=empty('circle-alert','Unable to connect','Check that the local engine is running.','<button data-refresh>Try again</button>');}
  finally{refreshPending=false;}
}
function navigate(next){view=next;groupId=null;search='';$('#screen').innerHTML='';render();refresh(true);}
function hydrateIcons(){document.querySelectorAll('[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon);el.removeAttribute('data-icon');});}
// Patch changed nodes in the runtime view, preserving focus and unchanged rows.
function updateRegion(root,html){
  const template=document.createElement('template');template.innerHTML=html;
  function key(node){if(node.nodeType!==Node.ELEMENT_NODE)return '';const attrs=[...node.attributes].filter(a=>a.name.startsWith('data-tg-')||a.name.startsWith('data-work-')||a.name==='data-preserve-draft'||a.name==='data-task-row'||a.name==='data-account-row');return attrs.map(a=>a.name+'='+a.value).join('|');}
  function patch(parent,next){
    for(let i=0;i<next.childNodes.length;i++){
      const incoming=next.childNodes[i];let current=parent.childNodes[i];
      if(!current){parent.append(incoming.cloneNode(true));continue;}
      const incomingKey=key(incoming);
      if(incomingKey&&incomingKey!==key(current)){
        const existing=[...parent.childNodes].slice(i+1).find(node=>key(node)===incomingKey);
        parent.insertBefore(existing||incoming.cloneNode(true),current);current=parent.childNodes[i];
      }else if(!incomingKey&&key(current)){current.replaceWith(incoming.cloneNode(true));continue;}
      if(current.isEqualNode(incoming))continue;
      if(current.nodeType===Node.ELEMENT_NODE&&current.hasAttribute('data-preserve-draft')&&current.getAttribute('data-preserve-draft')===incoming.getAttribute?.('data-preserve-draft'))continue;
      if(current.nodeType!==incoming.nodeType||current.nodeName!==incoming.nodeName){current.replaceWith(incoming.cloneNode(true));continue;}
      if(current.nodeType===Node.TEXT_NODE){current.nodeValue=incoming.nodeValue;continue;}
      if(current.nodeType!==Node.ELEMENT_NODE)continue;
      for(const attr of [...current.attributes])if(!incoming.hasAttribute(attr.name))current.removeAttribute(attr.name);
      for(const attr of incoming.attributes)if(current.getAttribute(attr.name)!==attr.value)current.setAttribute(attr.name,attr.value);
      patch(current,incoming);
    }
    while(parent.childNodes.length>next.childNodes.length)parent.lastChild.remove();
  }
  patch(root,template.content);hydrateTabs();
}
function hydrateTabs(){document.querySelectorAll('[role=tab]').forEach(el=>el.tabIndex=el.getAttribute('aria-selected')==='true'?0:-1);}
document.addEventListener('keydown',event=>{
  const tab=event.target.closest('[role=tab]'),list=tab?.closest('[role=tablist]');
  if(!list||tab.hasAttribute('data-settings-tab')||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();const tabs=[...list.querySelectorAll('[role=tab]')],index=tabs.indexOf(tab);
  const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
  const selected=tabs[next],selector=[...selected.attributes].find(a=>a.name.startsWith('data-'));
  selected.click();queueMicrotask(()=>{if(selector)document.querySelector(`[${selector.name}="${CSS.escape(selector.value)}"]`)?.focus();else selected.focus();hydrateTabs();});
});
async function confirmAction(title,description,label='Delete'){
  return new Promise(resolve=>{const d=dialog(`<form><h2>${esc(title)}</h2><p class="help">${esc(description)}</p><div class="modal-actions"><button type="button" data-cancel autofocus>Cancel</button><button class="danger" type="submit">${esc(label)}</button></div></form>`);let accepted=false;d.querySelector('[data-cancel]').onclick=()=>d.close();d.querySelector('form').onsubmit=e=>{e.preventDefault();accepted=true;d.close();};d.addEventListener('close',()=>resolve(accepted));});
}
function formData(form){const data=Object.fromEntries(new FormData(form));form.querySelectorAll('input[type=checkbox][name]:not(:disabled)').forEach(x=>data[x.name]=x.checked);return data;}
function feed(){return `<div class="notice">This feed contains observations from your tasks. Deal discovery requires your own ASIN list; no private Refract feed is connected.</div><div class="panel">${state.feed.length?table(['Product','Offer price','Seller / stock','Source','Actions'],[...state.feed].reverse().map(p=>`<tr><td>${esc(p.title)}<small class="mono">${esc(p.asin)} · ${esc(time(p.at))}</small></td><td>${p.price===null?'Unknown':p.price.toFixed(2)}<small>Before shipping and tax</small></td><td>${esc(p.seller)}<small>${p.available?'Available':'Unavailable'} · ${esc(p.condition)}</small></td><td>${badge(p.simulation?'simulation':'live')}</td><td><button data-copy="${esc(p.asin+(p.offer_id?';'+(p.price??'')+';'+p.offer_id:''))}">Copy input</button></td></tr>`)):empty('⌁','Listening starts with a task','Start a task group to see product availability, prices and offer IDs here.')}</div>`;}
function checkouts(){const rows=state.checkouts.filter(x=>historyFilter==='all'||(historyFilter==='simulation')===x.simulation);return `<div class="panel"><div class="toolbar"><h2>Order activity</h2><span class="spacer"></span><div class="filters">${['all','live','simulation'].map(f=>`<button data-filter="${f}" class="${f===historyFilter?'selected':''}">${f[0].toUpperCase()+f.slice(1)}</button>`).join('')}</div></div>${rows.length?table(['Product','Quantity','Total','Result','Time'],[...rows].reverse().map(x=>`<tr><td>${esc(x.title)}<small>${esc(x.asin)}${x.order_id?' · '+esc(x.order_id):''}</small></td><td>${x.quantity}</td><td>${x.total===null?'Check retailer':esc(x.currency)+' '+x.total.toFixed(2)}</td><td>${badge(x.status)}</td><td>${esc(time(x.at))}</td></tr>`)):empty('▤','No checkouts yet','Completed simulations and detected retailer order confirmations appear here. Cart additions are never counted as orders.')}</div>`;}
function events(){return `<div class="panel">${state.events.length?table(['Time','Task','Status','Message'],state.events.map(e=>`<tr><td>${esc(time(e.at))}</td><td class="mono">${esc(e.task_id.slice(0,8))}</td><td>${badge(e.status)}</td><td><small>${esc(e.message)}</small></td></tr>`)):empty('≡','A clean slate','Task status changes and errors will appear here as you run your first group.')}</div>`;}
function input(key,label,value='',type='text',extra=''){return `<label for="f-${key}">${label}</label><input id="f-${key}" name="${key}" type="${type}" value="${esc(value)}" ${extra}>`;}
function check(key,label,value){return `<label><input type="checkbox" name="${key}" ${value?'checked':''}>${label}</label>`;}
function select(key,label,values,value){return `<label for="f-${key}">${label}</label><select id="f-${key}" name="${key}">${values.map(([v,l])=>`<option value="${esc(v)}" ${v===value?'selected':''}>${esc(l)}</option>`).join('')}</select>`;}
function exportCSV(){const columns=['at','asin','title','quantity','total','currency','status','simulation','order_id'];const cell=v=>'"'+String(v??'').replace(/^[=+@\-]/,"'"+'$&').replaceAll('"','""')+'"';const csv=[columns.join(','),...state.checkouts.map(row=>columns.map(c=>cell(row[c])).join(','))].join('\r\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='retail-desk-checkouts.csv';a.click();URL.revokeObjectURL(url);}

function openEditor(kind,id){
  if(!kind)return;
  const value=state[kind]?.find(x=>x.id===id)||{};editing={kind,id};$('#form-error').textContent='';
  $('#modal-title').textContent=(id?'Edit ':'Add ')+({accounts:'account',profiles:'profile',proxies:'proxy list',input_lists:'product list',mailboxes:'mailbox',solvers:'connection'}[kind]||kind);
  let html='';
  if(kind==='accounts')html=accountFields(value);
  if(kind==='proxies')html=input('name','List name',value.name||'','text','required')+`<label for="proxy-entries">Connections</label><textarea id="proxy-entries" name="entries" ${id?'':'required'} placeholder="host:port"></textarea><p class="help">${id?'Leave connections blank to keep the saved list.':'One connection per line. Credentials stay encrypted.'}</p>`;
  $('#fields').innerHTML=kind==='accounts'?html:featureForm(kind,value,html);
  if(kind==='accounts')syncAccountFingerprintFields($('#editor'));
  $('#editor [type=submit]').textContent=kind==='accounts'?(id?'Save account':'Create Account'):'Save';
  $('#modal').showModal();decorateResourceEditor(kind,id);
}
$('#editor').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget,data=formData(form),{kind,id}=editing;
  if(!serializeFeature(kind,id,data,form))return;
  if(kind==='accounts'){
    data.fingerprint_overrides=collectAccountFingerprint(form);Object.assign(data,collectGeneratedFingerprint(form));
    for(const [key] of accountFingerprintOptions)delete data['account_'+key];
    data.purchase_cooldown_days=Number(data.purchase_cooldown_days||0);
    data.name=data.name||data.email;data.account_type=data.business?'business':'personal';delete data.business;
    if(data.proxy_mode==='direct'){data.proxy='';data.proxy_list_id='';}
    else if(data.proxy_mode==='list'){data.proxy='';if(!data.proxy_list_id){$('#form-error').textContent='Choose a proxy list.';return;}}
    else{data.proxy_list_id='';if(id&&!data.proxy)delete data.proxy;}
    delete data.proxy_mode;
    for(const key of ['password','totp_secret','cvv'])if(id&&!data[key])delete data[key];
    if(data.clear_totp)data.totp_secret='';if(data.clear_cvv)data.cvv='';delete data.clear_totp;delete data.clear_cvv;
  }
  if(kind==='proxies'&&id&&!data.entries)delete data.entries;
  const submit=form.querySelector('[type=submit]');submit.disabled=true;
  try{await api(kind+(id?'/'+id:''),id?'PUT':'POST',data);$('#modal').close();await refresh(true);toast('Saved');}
  catch(error){$('#form-error').textContent=error.message;}finally{submit.disabled=false;}
});
$('#primary').onclick=()=>{if(view==='task_groups')editTaskGroup();else if(view==='checkouts')exportCSV();else openEditor(view);};
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  try{
    if(button.hasAttribute('data-close')){$('#modal').close();return;}
    if(button.hasAttribute('data-refresh')){await refresh(true);return;}
    if(button.dataset.view){navigate(button.dataset.view);return;}
    if(button.dataset.copy){await navigator.clipboard.writeText(button.dataset.copy);toast('Copied');}
    if(button.dataset.filter){historyFilter=button.dataset.filter;render();}
    if(button.dataset.account){button.disabled=true;await api(`accounts/${button.dataset.account}/${button.dataset.op}`,'POST');await refresh(true);if(button.dataset.op==='login')toast('Complete sign-in in the browser window. Your session saves automatically.');}
    if(button.dataset.action==='edit')openEditor(button.dataset.kind,button.dataset.id);
    if(button.dataset.action==='delete'&&await confirmAction('Delete this record?','Existing group assignments must be removed first.')){await api(`${button.dataset.kind}/${button.dataset.id}`,'DELETE');await refresh(true);}
    if(button.dataset.action==='create-account')openEditor('accounts');
    if(button.dataset.action==='create-proxy')openEditor('proxies');
  }catch(error){toast(error.message);}finally{button.disabled=false;}
});
document.addEventListener('change',event=>{
  const el=event.target;
  if(el.name==='proxy_mode'){$('#account-proxy-list').hidden=el.value!=='list';$('#account-proxy-input').hidden=el.value!=='input';}
});
// Poll only the visible workspace, without overlapping requests or unchanged DOM replacement.
setInterval(()=>refresh(),2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
window.addEventListener('DOMContentLoaded',()=>refresh(true));
