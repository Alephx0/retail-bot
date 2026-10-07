'use strict';
let taskFilter = '', accountMode = 'single';
const originalEditor = openEditor;
function compactProduct(text){const first=(text||'').split('\n').filter(Boolean)[0]||'No input';const asin=first.match(/\/(?:dp|gp\/product)\/([a-z0-9]{10})/i);return asin?asin[1].toUpperCase():first.split(';')[0].replace(/^https?:\/\/(www\.)?/,'');}
function matchesStat(t, filter){
  if(!filter)return true;
  if(filter==='running')return state.active.includes(t.id);
  const states={carted:['carted','review','checkout','submitting'],queue:['in_queue'],passed:['passed'],success:['completed'],failed:['error']};
  return (states[filter]||[]).includes(t.status);
}
taskDetail = function(){
  const g=state.groups.find(x=>x.id===groupId);if(!g){groupId=null;return groups();}
  const tasks=state.tasks.filter(t=>t.group_id===g.id), visible=tasks.filter(t=>matchesStat(t,taskFilter));
  const amazon=g.retailer==='amazon';
  return `<div class="group-workspace"><aside class="group-settings"><button class="back" data-action="back">← View all groups</button><form id="group-settings" data-id="${g.id}"><h3>Group Info</h3>${input('name','Group name',g.name,'text','required maxlength="100"')}<label>Group site</label><p>${esc(retailerName(g.retailer))}</p><h3>Monitor</h3><label for="monitor-products">Monitor input</label><textarea id="monitor-products" name="products" rows="3" placeholder="${amazon?'ASIN;max price;offer ID, one per line':'Product ID or URL, one per line'}">${esc(g.products)}</textarea><p class="help">${amazon?'One ASIN or URL per line. Optional price and offer ID separated by semicolons.':'Live adapter planned; simulation is available.'}</p>${select('input_list_id','Input list',[['','Use monitor input'],...state.input_lists.filter(x=>x.retailer===g.retailer).map(x=>[x.id,x.name])],g.input_list_id||'')}${select('monitor_proxy_id','Monitor proxy list',[['','Use task connection'],...state.proxies.map(x=>[x.id,x.name])],g.monitor_proxy_id||'')}${input('delay_ms','Monitor delay (ms)',g.delay_ms??4500,'number','min="3500" required')}${amazon?input('offer_id','Offer ID (optional)',g.offer_id||'')+check('allow_used','Allow Used Products',g.allow_used)+check('notify_offer','Notify when Offer ID Found',g.notify_offer)+input('max_errors','Max Checkout Errors',g.max_errors??5,'number','min="1" max="30"'):''}<details><summary>Price & checkout limits</summary>${input('max_total','Maximum order total',g.max_total??100,'number','min="0" step="0.01" required')}${input('max_price','Maximum item price',g.max_price??'','number','min="0" step="0.01"')}${check('allow_third_party','Allow third-party sellers',g.allow_third_party)}${select('mode','Monitor mode',[['restock','Restocks'],['deals','Deals']],g.mode)}${check('only_freebies','Only freebies in deals mode',g.only_freebies)}${input('min_discount','Minimum discount (%)',g.min_discount??0,'number','min="0" max="100"')}${input('min_savings','Minimum savings',g.min_savings??0,'number','min="0" step="0.01"')}</details><button class="primary save-group" type="submit">Save settings</button></form><h3>Statistics</h3><div class="group-statistics">${[['running','Running'],['carted','Carted'],['queue','In Queue'],['passed','Passed'],['success','Success'],['failed','Failed']].map(([key,label])=>`<button data-stat="${key}" class="${taskFilter===key?'selected':''}" aria-pressed="${taskFilter===key}">${label}<strong>${tasks.filter(t=>matchesStat(t,key)).length}</strong></button>`).join('')}</div><h3>Schedule <button data-schedule="${g.id}">Configure</button></h3><p class="help">${g.schedule?.auto_start?'Starts on app launch. ':''}${g.schedule?.slots?.length||0} time slots · device local time</p><h3>Group Highlight Color</h3><input type="color" aria-label="Group highlight color" data-highlight="${g.id}" value="${esc(g.highlight||'#64d9ad')}"><button class="danger" data-action="delete" data-kind="groups" data-id="${g.id}">Delete group</button></aside><section class="task-panel panel"><div class="toolbar"><h2>${esc(g.name)}</h2><span class="badge">${visible.length}/${tasks.length}</span><span class="spacer"></span><button data-action="bulk" data-op="start">Start ${selected.size?'selected':'all'}</button><button data-action="bulk" data-op="stop">Stop ${selected.size?'selected':'all'}</button><button data-action="create-task">+ Tasks</button><button data-action="bulk" data-op="delete" aria-label="Delete selected or all tasks">Delete</button></div>${taskFilter?`<div class="filter-notice">Filtered by ${esc(taskFilter)} <button data-stat="">Show all tasks</button></div>`:''}${visible.length?table(['<input type="checkbox" id="select-all" aria-label="Select all tasks">','Account / proxy','Product','Qty','Status','Actions'],visible.map(t=>`<tr><td><input class="row-check" type="checkbox" data-select="${t.id}" ${selected.has(t.id)?'checked':''} aria-label="Select task"></td><td title="${esc(t.account_id?name('accounts',t.account_id):'Simulation')}"><span class="clip">${esc(t.account_id?name('accounts',t.account_id):'Simulation')}</span><small class="clip">${t.simulation?'Simulation ? ':''}${esc(t.use_account_proxy?'Account proxy':t.proxy_id?name('proxies',t.proxy_id):'Direct')}</small>${t.profile_id?`<small class="clip" title="${esc(name('profiles',t.profile_id))}">${esc(name('profiles',t.profile_id))}</small>`:''}</td><td title="${esc(groupProducts(g))}"><span class="clip mono">${esc(compactProduct(groupProducts(g)))}</span><small>${Math.max(1,groupProducts(g).split('\n').filter(Boolean).length)} input(s)</small></td><td>${t.quantity}</td><td title="${esc(t.message)}">${badge(t.status||'idle')}<small class="clip">${esc(t.message)}</small></td><td><div class="actions">${state.active.includes(t.id)?`<button data-task="${t.id}" data-op="${['attention','review'].includes(t.status)?'resume':'stop'}">${['attention','review'].includes(t.status)?'Resume':'Stop'}</button>`:`<button data-task="${t.id}" data-op="start">Start</button>`}<button data-task-details="${t.id}" aria-label="Task details">•••</button></div></td></tr>`)):empty('▷',taskFilter?'No tasks match this filter':'No tasks yet',taskFilter?'Choose Show all tasks to clear the filter.':'Create tasks to get started.')}<div class="task-foot">Hover a shortened value for the full text. Open ••• for task details and actions.</div></section></div>`;
};
function accountFields(v={}){
  const health=(state.browser_health||[]).find(x=>x.account_id===v.id);
  const policy=select('purchase_cooldown_days','Minimum days between orders',[[0,'Off'],...[2,3,4,5,6,7].map(n=>[n,`${n} days`])],v.purchase_cooldown_days||0)
    +'<p class="help">Applies to recorded live orders across this account. Tasks stop before opening a browser during cooldown; start again after the displayed time. Does not include purchases made outside this app.</p>'
    +(health?`<p class="help">Browser health: ${esc(health.status)}${health.drift?.length?' — '+esc(health.drift.join(', ')):''}</p>`:'');
  return baseAccountFields(v).replace('<div id="single-account">','<div id="single-account">'+policy+accountFingerprintFields(v));
}
function baseAccountFields(v={}){
  const amazon=(v.retailer||'amazon')==='amazon';
  return `${!v.id?'<div class="form-tabs"><button type="button" data-account-mode="single" class="selected">Single Input</button><button type="button" data-account-mode="mass">Mass Input</button></div>':''}${select('retailer','Site',retailerOptions(),v.retailer||'amazon')}<div id="single-account">${input('email','Login username / email',v.email||'','text','required autocomplete="off"')}${secret('password','Password',v.has_password)}${select('proxy_mode','Account proxy',[['direct','No proxy'],['list','Proxy List'],['input','Input Proxy']],v.proxy_list_id?'list':v.has_proxy?'input':'direct')}<div id="account-proxy-list" ${v.proxy_list_id?'':'hidden'}>${select('proxy_list_id','Proxy List',[['','Choose a proxy list'],...state.proxies.map(p=>[p.id,p.name])],v.proxy_list_id||'')}</div><div id="account-proxy-input" ${v.has_proxy?'':'hidden'}>${secret('proxy','Input Proxy',v.has_proxy)}</div><div id="amazon-account" ${amazon?'':'hidden'}>${secret('totp_secret','2FA Secret (authenticator)',v.has_totp)}${check('business','Business Account',v.account_type==='business')}${secret('cvv','Card CVV',v.has_cvv)}<p class="help">For supported Amazon card verification. Bank approval may still require the browser.</p></div><details><summary>Account group & verification</summary>${input('group','Account group',v.group||'Personal')}${select('mailbox_id','IMAP mailbox',[['','None'],...state.mailboxes.map(m=>[m.id,m.name])],v.mailbox_id||'')}${select('solver_id','CAPTCHA provider',[['','Manual browser'],...state.solvers.map(s=>[s.id,s.name])],v.solver_id||'')}${check('clear_totp','Clear saved authenticator secret',false)}${check('clear_cvv','Clear saved CVV',false)}</details></div><div id="mass-account" hidden><label for="mass-lines">Accounts — one per line</label><textarea id="mass-lines" name="mass_text" rows="8" spellcheck="false" placeholder="login:password&#10;login:password;proxy;secret;cvv" disabled></textarea><p class="help">Most accounts only need login:password. Optional: ;proxy;secret and, on Amazon, ;cvv. Up to 100 accounts; all lines are checked before saving.</p></div>`;
}
function taskFields(v={}){
  const g=state.groups.find(x=>x.id===(v.group_id||groupId));
  const accounts=state.accounts.filter(a=>a.retailer===(g?.retailer||'amazon'));
  return `<input type="hidden" name="group_id" value="${esc(g?.id||'')}">${select('simulation','Execution',[['true','Simulation'],['false','Live']],String(v.simulation??true))}${select('account_id','Account',[['','None (simulation)'],...accounts.map(a=>[a.id,a.name])],v.account_id||'')}${!v.id?select('account_group_scope','Or account group',[['','Use selected account'],...[...new Set(accounts.map(a=>a.group||'Personal'))].map(g=>[g,g])],''):''}${check('use_account_proxy','Use Account Proxy',v.use_account_proxy)}${select('proxy_id','Task proxy list',[['','Direct'],...state.proxies.map(p=>[p.id,p.name])],v.proxy_id||'')}${g?.retailer==='amazon'?check('force_free_shipping','Force Free Shipping (slower)',v.force_free_shipping)+check('auto_open_3ds','Auto Open Browser for 3DS',v.auto_open_3ds)+'<p class="help">Browser focus for Amex SafeKey / Capital One. Amazon CVV verification uses the account CVV.</p>':''}${input('retry_delay_ms','Retry delay (ms)',v.retry_delay_ms??3500,'number','min="1000" required')}<div class="form-grid">${!v.id?input('task_count','Task qty (per account)',1,'number','min="1" max="100" required'):''}${input('quantity','Item quantity',v.quantity??1,'number','min="1" max="30" required')}</div>${select('checkout_mode','Task behavior',[['review','Cart + browser review'],['automatic','Automatic checkout'],['monitor','Monitor only']],v.checkout_mode||'review')}<details><summary>Advanced</summary>${select('solver_id','Solver',[['','Use account provider'],...state.solvers.map(s=>[s.id,s.name])],v.solver_id||'')}${input('scheduled_at','Individual start (local time)',v.scheduled_at?new Date(new Date(v.scheduled_at)-new Date(v.scheduled_at).getTimezoneOffset()*60000).toISOString().slice(0,16):'','datetime-local')}</details><p class="help">Live tasks sharing an account queue automatically to prevent shared-cart conflicts. Automatic checkout can place a real order within the group budget.</p>`;
}
const originalTaskFields = taskFields;
taskFields = function(v={}) {
  let html = originalTaskFields(v);
  const g = state.groups.find(x => x.id === (v.group_id || groupId));
  html = html.replace('<select id="f-checkout_mode" name="checkout_mode">', '<select id="f-checkout_mode" name="checkout_mode"><option value="quote" '+(v.checkout_mode==='quote'?'selected':'')+'>Checkout total only (no purchase)</option>');
  if (g?.retailer === 'amazon' && !html.includes('name="use_buy_now"')) {
    html = html.replace('<details><summary>Advanced</summary>', check('use_buy_now','Amazon: use Buy Now when available',v.use_buy_now)+'<p class="help">Buy Now skips the shared cart when offered; otherwise the task uses the cart. Final item, quantity, seller, price and total are still verified.</p><details><summary>Advanced</summary>');
  }
  return html;
};
const originalTaskDetail = taskDetail;
taskDetail = function(){
  let html = originalTaskDetail();
  for(const id of state.active){
    if(!state.tasks.some(t=>t.id===id&&t.group_id===groupId))continue;
    html=html.replace(`<button data-task-details="${id}"`, `<button data-live-view="${id}">View live</button><button data-take-control="${id}">Take Control</button><button data-task-details="${id}"`);
  }
  const taskIds = new Set(state.tasks.filter(t=>t.group_id===groupId).map(t=>t.id));
  const quotes = (state.quotes||[]).filter(q=>taskIds.has(q.task_id)).slice(-3).reverse();
  if(!quotes.length)return html;
  const panel = `<div class="quote-panel"><h3>Latest checkout prices</h3>${quotes.map(q=>`<div class="quote-result"><strong>${esc(q.currency||'USD')} ${Number(q.total).toFixed(2)}</strong> for ${Number(q.quantity)} item(s) · no order placed<small>${(q.price_components||[]).map(c=>`${esc(c.label)}: ${Number(c.amount).toFixed(2)}`).join(' · ')}</small></div>`).join('')}</div>`;
  return html.replace('<div class="task-foot">', panel+'<div class="task-foot">');
};
function openBrowserView(scope,id,control=false){
  if(control && (scope!=='tasks' || !state.active.includes(id))){toast('Take Control is available for running tasks.');return;}
  const d=document.createElement('dialog');
  d.className='live-view-dialog';
  d.innerHTML=`<h2>${control?'Take Control':'Live view'} · ${scope==='accounts'?'account':'task'}</h2><p data-live-status>Connecting to the headless browser…</p><img data-live-image tabindex="0" alt="Current browser page">${control?'<p class="help">Click the page, then type on your keyboard. For pasted passwords or codes, use the private field below; it clears after sending. Native passkey or OS dialogs may require a separate visible browser and cannot be transferred without losing in-memory page state.</p><div class="browser-input"><input type="password" data-browser-text autocomplete="off" aria-label="Text to type into the focused website field" placeholder="Paste text for focused field"><button type="button" data-browser-send>Send text</button><button type="button" data-browser-tab>Tab</button><button type="button" data-browser-enter>Enter</button></div>':''}<div class="modal-actions"><button type="button" data-live-close>${control?'Return to task':'Close'}</button></div>`;
  document.body.append(d);
  let timer,controller;
  const image=d.querySelector('[data-live-image]');
  const status=d.querySelector('[data-live-status]');
  const input=async action=>{try{await api('browser/'+scope+'/'+encodeURIComponent(id)+'/input','POST',action);}catch(error){status.textContent=error.message;}};
  if(control){
    image.style.cursor='crosshair';
    image.addEventListener('click',e=>{const box=image.getBoundingClientRect(),width=image.naturalWidth,height=image.naturalHeight,scale=Math.min(box.width/width,box.height/height),w=width*scale,h=height*scale,left=box.left+(box.width-w)/2,top=box.top+(box.height-h)/2,x=(e.clientX-left)/scale,y=(e.clientY-top)/scale;if(x>=0&&y>=0&&x<width&&y<height)input({kind:'click',x,y});image.focus();});
    image.addEventListener('keydown',e=>{const key=e.key===' '?'Space':e.key;if(e.ctrlKey||e.altKey||e.metaKey)return;if(key.length===1){e.preventDefault();input({kind:'text',text:key});}else if(['Enter','Tab','Backspace','Delete','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Space'].includes(key)){e.preventDefault();input({kind:'key',key});}});
    image.addEventListener('paste',e=>{const value=e.clipboardData?.getData('text');if(value){e.preventDefault();input({kind:'text',text:value});}});
    image.addEventListener('wheel',e=>{e.preventDefault();input({kind:'scroll',delta:Math.max(-1000,Math.min(1000,e.deltaY))});},{passive:false});
    d.querySelector('[data-browser-send]').onclick=()=>{const field=d.querySelector('[data-browser-text]');if(field.value)input({kind:'text',text:field.value});field.value='';image.focus();};
    d.querySelector('[data-browser-tab]').onclick=()=>{input({kind:'key',key:'Tab'});image.focus();};
    d.querySelector('[data-browser-enter]').onclick=()=>{input({kind:'key',key:'Enter'});image.focus();};
  }
  const close=()=>{clearTimeout(timer);controller?.abort();d.close();d.remove();};
  d.querySelector('[data-live-close]').onclick=close;
  d.addEventListener('cancel',e=>{e.preventDefault();close();});
  d.showModal();
  async function update(){
    if(!d.open)return;
    controller=new AbortController();
    try{
      const response=await fetch('/api/browser/'+scope+'/'+encodeURIComponent(id)+'/frame',{headers:{'X-Retail-Client':'dashboard'},cache:'no-store',signal:controller.signal});
      if(!response.ok)throw new Error(response.status===409?(scope==='accounts'?'Account browser closed or session saved.':'Waiting for the task browser or navigation…'):'Live view is unavailable');
      const blob=await response.blob();
      const frame=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(blob);});
      if(!d.open)return;
      image.src=frame;
      status.textContent=scope==='tasks'?(state.tasks.find(t=>t.id===id)?.message||'Task browser is active'):'Use this browser to finish signing in. The account session saves automatically after verification.';
    }catch(error){if(error.name!=='AbortError'&&d.open)d.querySelector('[data-live-status]').textContent=error.message;}
    if(d.open)timer=setTimeout(update,1200);
  }
  update();
}
function openTaskLiveView(id){openBrowserView('tasks',id,false);}
document.addEventListener('click',event=>{const button=event.target.closest('[data-live-view],[data-take-control]');if(button){if(button.dataset.liveView)openTaskLiveView(button.dataset.liveView);else openBrowserView('tasks',button.dataset.takeControl,true);}});
document.addEventListener('click',event=>{const button=event.target.closest('button[data-task][data-op="focus"]');if(button&&!state.settings?.[0]?.show_browser_window){event.stopImmediatePropagation();openBrowserView('tasks',button.dataset.task,true);}},true);
openEditor = function(kind,id){
  if(!['groups','accounts','tasks'].includes(kind)){$('#editor [type=submit]').textContent='Save';return originalEditor(kind,id);}
  if(kind==='groups'&&id){groupId=id;view='tasks';render();return;}
  const v=state[kind].find(x=>x.id===id)||{};editing={kind,id};accountMode='single';
  $('#modal-title').textContent=(id?'Edit ':'Create ')+({groups:'task group',accounts:'account',tasks:'tasks'}[kind]);$('#form-error').textContent='';
  $('#fields').innerHTML=kind==='groups'?input('name','Group name','','text','required maxlength="100"')+select('retailer','Group site',retailerOptions(),'amazon'):kind==='accounts'?accountFields(v):taskFields(v);
  $('#editor [type=submit]').textContent=kind==='groups'?'Create task group':kind==='accounts'?(id?'Save account':'Create Account'):(id?'Save task':'Create 1 task');
  if(kind==='accounts')syncAccountFingerprintFields($('#editor'));
  $('#modal').showModal();if(kind==='tasks')$('#f-proxy_id').disabled=!!v.use_account_proxy;
};
function formData(form){const data=Object.fromEntries(new FormData(form));form.querySelectorAll('input[type=checkbox]:not(:disabled)').forEach(x=>data[x.name]=x.checked);return data;}
document.addEventListener('submit',async event=>{
  const form=event.target;
  if(form.id==='group-settings'){
    event.preventDefault();const data=formData(form);for(const k of ['delay_ms','max_total','max_errors','min_discount','min_savings','min_price','retry_delay_ms','quantity','max_checkouts'])if(k in data)data[k]=Number(data[k]);data.max_price=data.max_price===''?null:Number(data.max_price);
    try{await api('groups/'+form.dataset.id,'PUT',data);delete form.dataset.dirty;document.activeElement.blur();await refresh();toast('Group settings saved');}catch(e){toast(e.message);}return;
  }
  if(form.id!=='editor'||!['groups','accounts','tasks'].includes(editing?.kind))return;
  event.preventDefault();event.stopImmediatePropagation();const {kind,id}=editing,data=formData(form),submit=form.querySelector('[type=submit]');submit.disabled=true;
  try{
    if(kind==='accounts'&&accountMode==='mass'){await api('account-batches/create','POST',{retailer:data.retailer,text:data.mass_text,folder_id:data.folder_id});}
    else {
      if(kind==='accounts'){
        data.fingerprint_overrides=collectAccountFingerprint(form);
        for(const [key] of accountFingerprintOptions)delete data['account_'+key];
        data.purchase_cooldown_days=Number(data.purchase_cooldown_days||0);
        data.name=data.email;data.account_type=data.business?'business':'personal';delete data.business;delete data.mass_text;
        if(data.proxy_mode==='direct'){data.proxy='';data.proxy_list_id='';}else if(data.proxy_mode==='list'){data.proxy='';if(!data.proxy_list_id)throw new Error('Choose a proxy list');}else {data.proxy_list_id='';if(id&&!data.proxy)delete data.proxy;}
        delete data.proxy_mode;
        for(const key of ['password','totp_secret','cvv'])if(id&&!data[key])delete data[key];
        if(data.clear_totp)data.totp_secret='';if(data.clear_cvv)data.cvv='';delete data.clear_totp;delete data.clear_cvv;
      }
      if(kind==='tasks'){
        data.proxy_id=data.proxy_id||'';data.quantity=Number(data.quantity);data.retry_delay_ms=Number(data.retry_delay_ms);data.task_count=Number(data.task_count||1);data.simulation=data.simulation==='true';data.scheduled_at=data.scheduled_at?new Date(data.scheduled_at).toISOString():null;
        if(!id&&(data.account_group_scope||data.account_id)){data.account_ids=data.account_id?[data.account_id]:[];await api('task-batches/create','POST',data);}
        else if(!id){for(let i=0;i<data.task_count;i++)await api('tasks','POST',data);}
        else await api('tasks/'+id,'PUT',data);
      }else {const result=await api(kind+(id?'/'+id:''),id?'PUT':'POST',data);if(kind==='groups'){groupId=result.id;taskFilter='';}}
    }
    $('#modal').close();await refresh();toast('Saved');
  }catch(e){$('#form-error').textContent=e.message;}finally{submit.disabled=false;}
},true);
document.addEventListener('dblclick',e=>{const card=e.target.closest('[data-group]');if(card){groupId=card.dataset.group;taskFilter='';selected.clear();render();}});
document.addEventListener('click',event=>{
  const b=event.target.closest('button');if(!b)return;
  if(b.id==='primary'&&view==='tasks'&&groupId){event.stopImmediatePropagation();openEditor('tasks');}
  if(b.hasAttribute('data-stat')){taskFilter=b.dataset.stat;selected.clear();render();}
  if(b.dataset.accountMode){accountMode=b.dataset.accountMode;const mass=accountMode==='mass';$('#single-account').hidden=mass;$('#mass-account').hidden=!mass;$('#single-account').querySelectorAll('input,select').forEach(x=>x.disabled=mass);$('#mass-lines').disabled=!mass;$('#mass-lines').required=mass;document.querySelectorAll('[data-account-mode]').forEach(x=>x.classList.toggle('selected',x===b));}
  if(b.dataset.schedule)openSchedule(b.dataset.schedule);
  if(b.dataset.taskDetails){const t=state.tasks.find(x=>x.id===b.dataset.taskDetails);const d=document.createElement('dialog');d.innerHTML=`<h2>Task details</h2><p>${esc(t.message)}</p><p>Account: ${esc(name('accounts',t.account_id))}<br>Profile: ${esc(name('profiles',t.profile_id))}</p><p>Stage: ${esc(t.state||t.status)}</p><div class="task-evidence">${(state.diagnostics||[]).filter(d=>d.task_id===t.id).map(d=>`<button data-diagnostic="${d.id}">View ${esc(d.action)}</button>`).join('')}</div><p>Retry: ${t.retry_delay_ms??3500} ms · Item quantity: ${t.quantity}</p><p class="full-input">${esc(groupProducts(state.groups.find(x=>x.id===t.group_id)))}</p><div class="actions"><button data-action="edit" data-kind="tasks" data-id="${t.id}">Edit</button>${state.active.includes(t.id)?'<button data-live-view="'+t.id+'">View live</button><button data-take-control="'+t.id+'">Take Control</button>':''}${state.settings?.[0]?.show_browser_window?`<button data-task="${t.id}" data-op="focus">Focus window</button>`:''}<button data-task="${t.id}" data-op="stop">Stop</button><button data-action="delete" data-kind="tasks" data-id="${t.id}">Delete</button><button data-dismiss>Close</button></div>`;document.body.append(d);d.addEventListener('click',e=>{if(e.target.closest('button')){d.close();d.remove();}});d.showModal();}
},true);
document.addEventListener('change',async e=>{
  const el=e.target;
  if(el.name==='retailer'&&editing?.kind==='accounts'&&$('#amazon-account'))$('#amazon-account').hidden=el.value!=='amazon';
  if(el.name==='proxy_mode'){$('#account-proxy-list').hidden=el.value!=='list';$('#account-proxy-input').hidden=el.value!=='input';}
  if(el.name==='use_account_proxy'&&$('#f-proxy_id'))$('#f-proxy_id').disabled=el.checked;
  if(el.dataset.highlight){try{await api('groups/'+el.dataset.highlight,'PUT',{highlight:el.value});await refresh();}catch(e){toast(e.message);}}
});
document.addEventListener('input',event=>{const settings=event.target.closest('#group-settings');if(settings)settings.dataset.dirty='true';if(editing?.kind!=='tasks'||!$('#modal').open)return;const f=$('#editor'),scope=f.elements.account_group_scope?.value;const site=state.groups.find(g=>g.id===f.elements.group_id.value)?.retailer;const accounts=scope?state.accounts.filter(a=>a.group===scope&&a.retailer===site).length:1;const count=accounts*Number(f.elements.task_count?.value||1);f.querySelector('[type=submit]').textContent=editing.id?'Save task':`Create ${count} task${count===1?'':'s'}`;});
function openSchedule(id){
  const g=state.groups.find(x=>x.id===id),s=g.schedule||{},d=document.createElement('dialog');
  d.innerHTML=`<form><h2>Group schedule</h2>${check('auto_start','Auto start on app launch',s.auto_start)}<p class="help">All times use this device’s local time. Keep the engine running for schedules.</p><div class="weekdays">${['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((day,i)=>`<label><input type="checkbox" name="day" value="${i}" ${s.days?.includes(i)?'checked':''}>${day}</label>`).join('')}</div><p class="help">No days selected: each slot runs once at its next occurrence. Stop earlier than start: stop the following day.</p><div class="schedule-slots"></div><button type="button" data-add-slot>+ Add Slot</button><p class="schedule-error" role="alert"></p><div class="modal-actions"><button type="button" data-cancel>Cancel</button><button type="submit" class="primary">Save</button></div></form>`;
  const add=(slot={start:'09:00',stop:'09:30'})=>{const row=document.createElement('div');row.className='schedule-slot';row.innerHTML=`<label>Start<input type="time" name="start" required value="${esc(slot.start)}"></label><label>Stop<input type="time" name="stop" required value="${esc(slot.stop)}"></label><button type="button" aria-label="Remove slot">×</button>`;row.querySelector('button').onclick=()=>row.remove();d.querySelector('.schedule-slots').append(row);};
  (s.slots||[]).forEach(add);d.querySelector('[data-add-slot]').onclick=()=>add();d.querySelector('[data-cancel]').onclick=()=>{d.close();d.remove();};d.querySelector('form').onsubmit=async e=>{e.preventDefault();const schedule={auto_start:d.querySelector('[name=auto_start]').checked,days:[...d.querySelectorAll('[name=day]:checked')].map(x=>Number(x.value)),slots:[...d.querySelectorAll('.schedule-slot')].map(row=>({start:row.querySelector('[name=start]').value,stop:row.querySelector('[name=stop]').value}))};try{await api('groups/'+id,'PUT',{schedule});d.close();d.remove();await refresh();toast('Schedule saved');}catch(err){d.querySelector('.schedule-error').textContent=err.message;}};
  document.body.append(d);d.showModal();
}
