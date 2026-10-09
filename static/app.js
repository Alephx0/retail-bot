'use strict';
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = {groups:[],tasks:[],accounts:[],proxies:[],events:[],checkouts:[],feed:[],settings:[],active:[],profiles:[],mailboxes:[],solvers:[],input_lists:[],retailers:[],proxy_health:[],harvesters:[]};
let view = 'tasks', groupId = null, search = '', editing = null, selected = new Set(), historyFilter = 'all';
const titles = {tasks:['Task groups','Every product. Every task. One place to stay in control.','＋ Create group'],accounts:['Accounts','Your retailer accounts, organized and ready to go.','＋ Add account'],proxies:['Proxy lists','Manage connections for your task browsers.','＋ Create list'],feed:['Product monitor','Availability and offers detected by your own running tasks.',''],checkouts:['Checkout history','Live confirmations and clearly labeled simulation results.','Export CSV'],events:['Activity log','A timestamped view of everything your tasks are doing.',''],settings:['Settings','Local preferences and notification delivery.','']};
Object.assign(titles, featureTitles);
async function api(path, method='GET', body) {
  const response = await fetch('/api/'+path,{method,headers:{'Content-Type':'application/json','X-Retail-Client':'dashboard'},body:body===undefined?undefined:JSON.stringify(body)});
  const data = await response.json();
  if(!response.ok) throw new Error(typeof data.detail === 'string'?data.detail:JSON.stringify(data.detail));
  return data;
}
let toastTimer;
function toast(message){$('#toast').textContent=message;$('#toast').style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').style.display='none',6000);}
function badge(status){const color=['completed','monitoring'].includes(status)?'green':['review','attention','scheduled','retrying'].includes(status)?'amber':['error'].includes(status)?'red':['carting','starting'].includes(status)?'blue':'';return `<span class="badge ${color}">${esc(status.replaceAll('_',' '))}</span>`;}
function name(kind,id){return state[kind].find(x=>x.id===id)?.name || '—';}
function time(value){return value?new Date(value).toLocaleString(): '—';}
function table(headers, rows){return `<div class="table-wrap"><table><thead><tr>${headers.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;}
function empty(icon,title,description,actions=''){return `<div class="empty"><div class="empty-icon">${icon}</div><h2>${title}</h2><p>${description}</p>${actions}</div>`;}
let stateLoaded=false;
async function refresh(){try{if(view==='task_groups'&&stateLoaded){if(taskGroupId){await loadTaskGroup(true);}else{state.task_groups=(await api('task-groups')).groups;render();}$('#connection').textContent='Connected';return;}state=await api('state');stateLoaded=true;processSounds();$('#connection').textContent='● Connected';render();}catch(e){$('#connection').textContent='● Disconnected';}}
// The application renderer is installed by redesign.js after shared helpers load.
function render(){}
function groups(){
  const filtered=state.groups.filter(g=>g.name.toLowerCase().includes(search.toLowerCase()));
  return `<div class="panel"><div class="toolbar"><h2>Your groups</h2><span class="badge">${state.groups.length}</span><span class="spacer"></span><input id="search" aria-label="Search groups" placeholder="⌕  Search groups" value="${esc(search)}"></div>${!state.groups.length?empty('▦','Your next find starts here','Create a group to monitor products, or try a simulation to explore the workflow without a retailer account.','<button class="primary" data-action="create-group">＋ Create your first group</button><button data-action="demo">Load simulation</button>'):''}</div>${state.groups.length?`<div class="group-grid">${filtered.map(g=>{const tasks=state.tasks.filter(t=>t.group_id===g.id),running=tasks.filter(t=>state.active.includes(t.id)).length;return `<article class="group-card" role="button" tabindex="0" data-group="${g.id}"><div class="group-top"><div class="amazon-logo">${esc(retailerName(g.retailer).slice(0,1))}</div><span class="badge ${running?'green':''}">${running?'● Running':'Idle'}</span></div><h3>${esc(g.name)}</h3><p>${esc(retailerName(g.retailer))} · ${g.mode==='deals'?'Freebies & deals':'Releases & restocks'}</p><div class="group-bottom"><span>${groupProducts(g).split('\n').filter(Boolean).length} products <span class="muted">/</span> ${tasks.length} tasks</span><strong>Open group ↗</strong></div></article>`;}).join('')}</div>`:''}<div class="notice">Choose a retailer, connect an account, and configure your monitor. Each retailer’s implementation status is listed in Retailers. Simulation never places an order.</div>`;
}
// Task group rendering is provided by workspace.js.
let taskDetail;
function feed(){return `<div class="notice">This feed contains observations from your tasks. Deal discovery requires your own ASIN list; no private Refract feed is connected.</div><div class="panel">${state.feed.length?table(['Product','Offer price','Seller / stock','Source','Actions'],[...state.feed].reverse().map(p=>`<tr><td>${esc(p.title)}<small class="mono">${esc(p.asin)} · ${esc(time(p.at))}</small></td><td>${p.price===null?'Unknown':p.price.toFixed(2)}<small>Before shipping and tax</small></td><td>${esc(p.seller)}<small>${p.available?'Available':'Unavailable'} · ${esc(p.condition)}</small></td><td>${badge(p.simulation?'simulation':'live')}</td><td><button data-copy="${esc(p.asin+(p.offer_id?';'+(p.price??'')+';'+p.offer_id:''))}">Copy input</button></td></tr>`)):empty('⌁','Listening starts with a task','Start a task group to see product availability, prices and offer IDs here.')}</div>`;}
function checkouts(){const rows=state.checkouts.filter(x=>historyFilter==='all'||(historyFilter==='simulation')===x.simulation);return `<div class="panel"><div class="toolbar"><h2>Order activity</h2><span class="spacer"></span><div class="filters">${['all','live','simulation'].map(f=>`<button data-filter="${f}" class="${f===historyFilter?'selected':''}">${f[0].toUpperCase()+f.slice(1)}</button>`).join('')}</div></div>${rows.length?table(['Product','Quantity','Total','Result','Time'],[...rows].reverse().map(x=>`<tr><td>${esc(x.title)}<small>${esc(x.asin)}${x.order_id?' · '+esc(x.order_id):''}</small></td><td>${x.quantity}</td><td>${x.total===null?'Check retailer':esc(x.currency)+' '+x.total.toFixed(2)}</td><td>${badge(x.status)}</td><td>${esc(time(x.at))}</td></tr>`)):empty('▤','No checkouts yet','Completed simulations and detected retailer order confirmations appear here. Cart additions are never counted as orders.')}</div>`;}
function events(){return `<div class="panel">${state.events.length?table(['Time','Task','Status','Message'],state.events.map(e=>`<tr><td>${esc(time(e.at))}</td><td class="mono">${esc(e.task_id.slice(0,8))}</td><td>${badge(e.status)}</td><td><small>${esc(e.message)}</small></td></tr>`)):empty('≡','A clean slate','Task status changes and errors will appear here as you run your first group.')}</div>`;}
function input(key,label,value='',type='text',extra=''){return `<label for="f-${key}">${label}</label><input id="f-${key}" name="${key}" type="${type}" value="${esc(value)}" ${extra}>`;}
function check(key,label,value){return `<label><input type="checkbox" name="${key}" ${value?'checked':''}>${label}</label>`;}
function select(key,label,values,value){return `<label for="f-${key}">${label}</label><select id="f-${key}" name="${key}">${values.map(([v,l])=>`<option value="${esc(v)}" ${v===value?'selected':''}>${esc(l)}</option>`).join('')}</select>`;}
function openEditor(kind,id){
  const v=state[kind].find(x=>x.id===id)||{};editing={kind,id};$('#form-error').textContent='';
  $('#modal-title').textContent=(id?'Edit ':'Create ')+({groups:'task group',accounts:'account',profiles:'profile',mailboxes:'IMAP mailbox',solvers:'solver connection',input_lists:'input list',proxies:'proxy list',tasks:'tasks'}[kind]);
  let html='';
  if(kind==='proxies') html=input('name','List name',v.name||'','text','required')+`<label for="f-entries">${id?'Replace connections (blank preserves saved list)':'Connections'}</label><textarea id="f-entries" name="entries" ${id?'':'required'} placeholder="host:port&#10;host:port:username:password"></textarea><p class="help">One HTTP proxy per line. Credentials are encrypted and not returned to the dashboard. Assignment remains stable for each running browser.</p>`;
  html=featureForm(kind,v,html);
  $('#fields').innerHTML=html;$('#modal').showModal();
}
$('#editor').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget, data=Object.fromEntries(new FormData(form));
  form.querySelectorAll('input[type=checkbox]').forEach(el=>data[el.name]=el.checked);
  const {kind,id}=editing;
  if(!serializeFeature(kind,id,data,form)) return;
  if(kind==='groups')for(const key of ['delay_ms','min_price','max_price','min_discount','min_savings','max_total','max_checkouts','max_errors'])data[key]=data[key]===''&&key==='max_price'?null:Number(data[key]);
  if(kind==='accounts'){if(id&&!data.proxy&&!data.clear_proxy)delete data.proxy;delete data.clear_proxy;}
  if(kind==='proxies'&&id&&!data.entries)delete data.entries;
  let count=1;
  if(kind==='tasks'){count=Number(data.count||1);delete data.count;data.quantity=Number(data.quantity);data.simulation=data.simulation==='true';data.scheduled_at=data.scheduled_at?new Date(data.scheduled_at).toISOString():null;if(!data.simulation)count=1;}
  const submit=form.querySelector('[type=submit]');submit.disabled=true;
  try{if(kind==='tasks'&&!id&&data.account_group_scope){const result=await api('task-batches/create','POST',data);toast(`Created ${result.created.length} account tasks`);}else{for(let i=0;i<count;i++){const record=await api(kind+(id?'/'+id:''),id?'PUT':'POST',data);if(kind==='groups')groupId=record.id;}}$('#modal').close();await refresh();toast('Saved');}catch(e){$('#form-error').textContent=e.message;}finally{submit.disabled=false;}
});
$('#primary').addEventListener('click',()=>{if(view==='checkouts')return exportCSV();openEditor({tasks:'groups',accounts:'accounts',proxies:'proxies',profiles:'profiles',mailboxes:'mailboxes',solvers:'solvers',input_lists:'input_lists'}[view]);});
document.addEventListener('click',async event=>{
  const b=event.target.closest('button,[data-group]');if(!b)return;
  try{
    if(b.hasAttribute('data-close')){$('#modal').close();return;}
    if(b.dataset.view){view=b.dataset.view;groupId=null;selected.clear();$('#screen').innerHTML='';render();return;}
    if(b.dataset.group)return;
    if(b.dataset.copy){await navigator.clipboard.writeText(b.dataset.copy);toast('Monitor input copied');return;}
    if(b.dataset.filter){historyFilter=b.dataset.filter;render();return;}
    if(b.dataset.task){b.disabled=true;await api(`tasks/${b.dataset.task}/${b.dataset.op}`,'POST');await refresh();return;}
    if(b.dataset.account){b.disabled=true;await api(`accounts/${b.dataset.account}/${b.dataset.op}`,'POST');await refresh();if(b.dataset.op==='login'){toast('Complete sign-in in the browser window. Your session saves automatically once verified.');}else toast('Session saved');return;}
    const action=b.dataset.action;
    if(action==='back'){groupId=null;selected.clear();render();}
    if(action==='create-group')openEditor('groups');
    if(action==='create-account')openEditor('accounts');
    if(action==='create-proxy')openEditor('proxies');
    if(action==='create-task')openEditor('tasks');
    if(action==='edit')openEditor(b.dataset.kind,b.dataset.id);
    if(action==='demo'){const result=await api('demo/load','POST');groupId=result.group_id;await refresh();toast('Simulation tasks created. Select Start all to try them.');}
    if(action==='delete'){if(!confirm('Delete this record? Associated tasks must be removed first.'))return;await api(`${b.dataset.kind}/${b.dataset.id}`,'DELETE');selected.delete(b.dataset.id);await refresh();}
    if(action==='bulk'){
      const tasks=state.tasks.filter(t=>t.group_id===groupId&&(!selected.size||selected.has(t.id)));
      if(b.dataset.op==='delete'&&!confirm(`Delete ${tasks.length} tasks?`))return;
      let errors=[];
      for(const t of tasks){try{await api(`tasks/${t.id}`+(b.dataset.op==='delete'?'':'/'+b.dataset.op),b.dataset.op==='delete'?'DELETE':'POST');}catch(e){errors.push(e.message);}}
      selected.clear();await refresh();if(errors.length)toast([...new Set(errors)].join('; '));
    }
  }catch(e){toast(e.message);}finally{b.disabled=false;}
});
document.addEventListener('keydown',event=>{if(event.target.matches('[data-group]')&&['Enter',' '].includes(event.key)){event.preventDefault();groupId=event.target.dataset.group;selected.clear();render();}});
document.addEventListener('input',event=>{if(event.target.id==='search'){search=event.target.value;const start=event.target.selectionStart;event.target.blur();render();$('#search').focus();$('#search').setSelectionRange(start,start);}});
document.addEventListener('change',event=>{
  if(event.target.dataset.select){event.target.checked?selected.add(event.target.dataset.select):selected.delete(event.target.dataset.select);render();}
  if(event.target.id==='select-all'){state.tasks.filter(t=>t.group_id===groupId).forEach(t=>event.target.checked?selected.add(t.id):selected.delete(t.id));event.target.blur();render();}
});
function exportCSV(){const columns=['at','asin','title','quantity','total','currency','status','simulation','order_id'];const cell=v=>'"'+String(v??'').replace(/^[=+@\-]/,"'"+'$&').replaceAll('"','""')+'"';const csv=[columns.join(','),...state.checkouts.map(row=>columns.map(c=>cell(row[c])).join(','))].join('\r\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='retail-desk-checkouts.csv';a.click();URL.revokeObjectURL(url);}
refresh();setInterval(refresh,1800);
