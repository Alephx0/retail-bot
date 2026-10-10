'use strict';
function createProfileTasks(){
  const group=state.groups.find(g=>g.id===groupId);if(!group){toast('Open a task group first');return;}
  const accounts=state.accounts.filter(a=>a.retailer===group.retailer),folders=state.folders||[];
  const d=dialog(`<form id="assignment-editor"><h2>Add tasks</h2><h3>Checkout Profile</h3>${tabs([['profile','Profile'],['profile_group','Profile Group']],'profile','data-assignment-profile')}<div data-profile-choice="profile">${select('profile_id','Profile',[['','None'],...state.profiles.map(p=>[p.id,p.name])],'')}</div><div data-profile-choice="profile_group" hidden>${select('profile_group_id','Profile Group',[['','Choose group'],...folders.filter(f=>f.resource_kind==='profiles').map(f=>[f.id,f.name])],'')}</div><h3>Account Assignment</h3>${check('match_profiles','Match Accounts to Profiles',false)}<p class="help">Matches by email for this retailer; saved relationships take priority. Turn off to choose an account or group manually.</p><div data-manual-accounts>${tabs([['account','Account'],['account_group','Account Group']],'account','data-assignment-account')}<div data-account-choice="account">${select('account_id','Account',[['','None (simulation)'],...accounts.map(a=>[a.id,a.name])],'')}</div><div data-account-choice="account_group" hidden>${select('account_group_id','Account Group',[['','Choose group'],...folders.filter(f=>f.resource_kind==='accounts').map(f=>[f.id,f.name])],'')}</div></div><div class="form-grid"><div>${input('count','Task quantity',1,'number','min="1" max="100" required')}</div><div>${input('quantity','Item quantity',group.quantity??1,'number','min="1" max="30" required')}</div></div>${select('distribution','Group assignment',[['sequential','Sequential'],['random','Random'],['one_to_one','One-to-one']],'sequential')}${select('simulation','Execution',[['true','Simulation'],['false','Live']],'true')}<p class="help">Task quantity is the total to create. Sequential and Random cycle through the selected groups; One-to-one uses each profile and account once.</p><div class="assignment-errors" role="alert"></div><div class="assignment-preview"></div><details><summary>Checkout & Network</summary>${monitorSelect(group)}${input('max_total','Maximum order total','','number',`min="0" step="0.01" placeholder="Group default: ${group.max_total}"`)}${check('use_buy_now','Use Buy Now when available',false)}${select('solver_id','Verification provider',[['','Use account provider'],...state.solvers.map(x=>[x.id,x.name])],'')}${select('checkout_mode','Task behavior',[['review','Cart + browser review'],['automatic','Automatic checkout'],['monitor','Monitor only'],['quote','Checkout total only']],'review')}${select('proxy_id','Task Proxy Group',[['','Direct'],...state.proxies.map(p=>[p.id,p.name])],'')}${check('use_account_proxy','Use Account Proxy',true)}${input('scheduled_at','Individual start time','','datetime-local')}${input('retry_delay_ms','Retry Delay',group.retry_delay_ms??3500,'number','min="1000" required')}${group.retailer==='amazon'?check('force_free_shipping','Force Free Shipping (slower)',false)+check('auto_open_3ds','Auto Open Browser for 3DS',false):''}</details><p class="help">Amazon uses the payment and address saved on its account. Profile assignment records your intended identity; it does not replace Amazon account defaults.</p><div class="modal-actions"><button type="button" data-cancel>Cancel</button><button type="submit" class="primary" disabled>Create tasks</button></div></form>`);
  const form=d.querySelector('form'),submit=form.querySelector('[type=submit]'),output=form.querySelector('.assignment-preview'),errors=form.querySelector('.assignment-errors');
  const seed=Math.floor(Math.random()*2147483647);
  let profileMode='profile',accountMode='account',preview=null,revision=0,lastPreviewInput='',readyInput='',saving=false,pending=false;
  function data(){
    const value=formData(form);value.group_id=group.id;value.seed=seed;
    for(const key of ['count','quantity','retry_delay_ms'])value[key]=Number(value[key]);
    value.simulation=value.simulation==='true';value.max_total=value.max_total===''?null:Number(value.max_total);
    if(value.scheduled_at){const date=new Date(value.scheduled_at);if(!Number.isFinite(date.getTime()))throw Error('Enter a valid individual start time.');value.scheduled_at=date.toISOString();}else value.scheduled_at=null;
    if(profileMode==='profile')delete value.profile_group_id;else delete value.profile_id;
    if(accountMode==='account')delete value.account_group_id;else delete value.account_id;
    return value;
  }
  function sync(){form.querySelector('[data-manual-accounts]').hidden=form.elements.match_profiles.checked;form.elements.proxy_id.disabled=form.elements.use_account_proxy.checked;}
  function failure(error){
    preview=null;readyInput='';submit.disabled=true;errors.textContent=error.message;
    output.innerHTML='<button type="button" data-refresh-preview>Retry preview</button>';
  }
  async function update(force=false){
    if(saving)return;
    let request,fingerprint;
    try{request=data();fingerprint=JSON.stringify(request);}catch(error){++revision;pending=false;lastPreviewInput='';failure(error);return;}
    if(!force&&fingerprint===lastPreviewInput)return;
    lastPreviewInput=fingerprint;const turn=++revision;pending=true;preview=null;readyInput='';submit.disabled=true;
    errors.textContent='';output.setAttribute('aria-busy','true');output.innerHTML='<p role="status">Updating assignments...</p>';
    try{
      const result=await api('assignments/preview','POST',request);
      if(turn!==revision||!d.isConnected||saving)return;
      if(profileMode==='profile_group'&&!request.profile_group_id)result.errors.push('Choose a profile group.');
      if(!request.match_profiles&&accountMode==='account_group'&&!request.account_group_id)result.errors.push('Choose an account group.');
      preview=result.errors.length?null:result.rows;readyInput=preview?fingerprint:'';
      output.innerHTML=table(['Task','Account','Profile'],result.rows.map(r=>`<tr><td>${r.index}</td><td>${esc(name('accounts',r.account_id))}<small>${esc(r.match_reason)}</small></td><td>${esc(name('profiles',r.profile_id))}</td></tr>`));
      errors.innerHTML=result.errors.map(e=>`<p>${esc(e)}</p>`).join('')+(result.errors.length?`<details><summary>Resolve account/profile mappings</summary>${accounts.map(a=>`<button type="button" data-link-account="${a.id}">${esc(a.name)}</button>`).join('')}<button type="button" data-refresh-preview>Refresh preview</button></details>`:'');
      submit.disabled=!!result.errors.length;submit.textContent=`Create ${result.rows.length} task${result.rows.length===1?'':'s'}`;
    }catch(error){if(turn===revision&&d.isConnected){lastPreviewInput='';failure(error);}}
    finally{if(turn===revision){pending=false;output.removeAttribute('aria-busy');}}
  }
  form.addEventListener('invalid',event=>{const details=event.target.closest('details');if(details)details.open=true;},true);
  form.oninput=()=>{sync();update();};form.onchange=()=>{sync();update();};
  form.onclick=e=>{
    const button=e.target.closest('button');if(!button)return;
    // Global action handlers must not re-enable controls while preview/creation is pending.
    e.stopPropagation();
    if(button.hasAttribute('data-cancel')){d.close();return;}
    if(button.dataset.linkAccount){linkAccount(button.dataset.linkAccount);return;}
    if(saving)return;
    if(button.dataset.assignmentProfile){profileMode=button.dataset.assignmentProfile;form.querySelectorAll('[data-profile-choice]').forEach(x=>x.hidden=x.dataset.profileChoice!==profileMode);}
    if(button.dataset.assignmentAccount){accountMode=button.dataset.assignmentAccount;form.querySelectorAll('[data-account-choice]').forEach(x=>x.hidden=x.dataset.accountChoice!==accountMode);}
    if(button.dataset.assignmentProfile||button.dataset.assignmentAccount){button.parentElement.querySelectorAll('button').forEach(x=>{x.classList.toggle('selected',x===button);x.setAttribute('aria-selected',String(x===button));});hydrateTabs();update();}
    if(button.hasAttribute('data-refresh-preview'))update(true);
  };
  form.onsubmit=async e=>{
    e.preventDefault();if(saving||pending)return;
    let request;try{request=data();}catch(error){failure(error);return;}
    if(!preview||JSON.stringify(request)!==readyInput){await update(true);return;}
    saving=true;submit.disabled=true;++revision;
    try{const result=await api('assignments/create','POST',{...request,preview});d.close();await refresh(true);toast(`Created ${result.created.length} tasks`);}
    catch(error){lastPreviewInput='';failure(error);}
    finally{saving=false;}
  };
  hydrateTabs();sync();update();
}
