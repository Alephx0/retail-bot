'use strict';
function accountFields(v={}){
  return `${select('retailer','Retailer',retailerOptions(),v.retailer||'amazon')}<div id="single-account">${input('name','Account label',v.name||'')}${input('email','Email or username',v.email||'','text','required autocomplete="off"')}${secret('password','Password (or sign in with the browser)',v.has_password)}<details><summary>Connection & verification</summary>${select('region','Region',[['US','United States'],['UK','United Kingdom'],['CA','Canada']],v.region||'US')}${select('proxy_mode','Connection',[['direct','Direct connection'],['list','Saved proxy list'],['input','Custom proxy']],v.proxy_list_id?'list':v.has_proxy?'input':'direct')}<div id="account-proxy-list" ${v.proxy_list_id?'':'hidden'}>${select('proxy_list_id','Proxy list',[['','Choose a proxy list'],...state.proxies.map(p=>[p.id,p.name])],v.proxy_list_id||'')}</div><div id="account-proxy-input" ${v.has_proxy?'':'hidden'}>${secret('proxy','Proxy',v.has_proxy)}</div>${secret('totp_secret','Authenticator secret',v.has_totp)}${secret('cvv','Card verification code',v.has_cvv)}${select('mailbox_id','Verification mailbox',[['','Manual verification'],...state.mailboxes.map(m=>[m.id,m.name])],v.mailbox_id||'')}${select('solver_id','Verification provider',[['','Manual browser'],...state.solvers.map(x=>[x.id,x.name])],v.solver_id||'')}${check('business','Business account',v.account_type==='business')}${select('purchase_cooldown_days','Minimum days between purchases',[[0,'Retailer defaults'],...[2,3,4,5,6,7].map(n=>[n,n+' days'])],v.purchase_cooldown_days||0)}${input('notes','Notes',v.notes||'')}${check('clear_totp','Remove saved authenticator secret',false)}${check('clear_cvv','Remove saved card verification code',false)}</details>${accountFingerprintFields(v)}</div>`;
}
function openBrowserView(scope,id,control=false){
  if(control && scope!=='group_attempts' && (scope!=='tasks' || !state.active.includes(id))){toast('Take Control is available for running tasks.');return;}
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
