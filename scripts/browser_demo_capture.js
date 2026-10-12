(() => {
  if (window !== top || !/^www\.amazon\.(com|co\.uk|ca)$/.test(location.hostname)) return;
  let enabled=false, finished=false, ready=false, queue=[], pending=[], commands=[], lastMove=0, lastDown=null, lastUp=null;
  const held=new Map();
  const pageKind=()=>/\/(?:dp|gp\/product)\//.test(location.pathname)?'product':/^\/s(?:\/|$)/.test(location.pathname)?'search':location.pathname==='/'?'home':'excluded';
  const allowed=()=>enabled&&!finished&&pageKind()!=='excluded'&&!document.querySelector('#ap_password,#ap_email,#ap_email_login,#auth-mfa-otpcode,#captchacharacters');
  let panel, label;
  const own=e=>e.composedPath().includes(panel);
  function kind(e){
    if(e.closest('#twotabsearchtextbox,#nav-search-submit-button'))return 'search';
    if(e.closest('[id^="variation_"],#twister,#twister_feature_div,#twister-plus-inline-twister'))return 'variant';
    if(e.closest('#seeMoreDetailsLink,#productOverview_feature_div,#productDetails_feature_div,#feature-bullets'))return 'details';
    if(e.closest('#customerReviews,#reviewsMedley,#cm-cr-dp-review-list,#acrCustomerReviewLink'))return 'reviews';
    if(e.closest('a[href*="/dp/"],a[href*="/gp/product/"]'))return 'product';
    return e.closest('a,button,input,select')?'control':'page';
  }
  function record(type, data={}){
    if(!allowed())return;
    queue.push({type,t:Date.now(),page:pageKind(),...data});
    if(queue.length>=100)flush();
  }
  function flush(){
    pending.push(...queue);queue=[];
    if(pending.length>1000)pending.splice(0,pending.length-1000);
  }
  // CDP reads the main-world buffer; browser bindings live in another world.
  window.__retailDemoDrain=()=>{flush();return {events:pending.splice(0),commands:commands.splice(0)};};
  window.__retailDemoSetState=state=>{
    const first=!ready;ready=true;finished=!state.recording;enabled=!state.paused;
    if(!enabled||finished){held.clear();lastUp=null;lastDown=null;}
    render();if(first)record('page');
  };
  function render(){const buttons=panel?.shadowRoot?.querySelectorAll('button');if(buttons){buttons.forEach(b=>b.disabled=finished);buttons[0].textContent=enabled?'Pause':'Resume';}if(label)label.textContent=!ready?'Connecting capture...':finished?'Capture finished - you can keep browsing':!enabled?'Capture paused':!allowed()?'Capture off on this page':'Recording mouse + search-key timing only';}
  addEventListener('pointermove',e=>{
    if(!e.isTrusted||own(e)||performance.now()-lastMove<8)return;
    lastMove=performance.now();record('move',{x:e.clientX,y:e.clientY,w:innerWidth,h:innerHeight});
  },true);
  addEventListener('pointerdown',e=>{if(e.isTrusted&&!own(e)&&allowed())lastDown=performance.now();},true);
  addEventListener('click',e=>{
    if(!e.isTrusted||own(e))return;
    const r=(e.target.closest('a,button,input,select,[role="radio"],.a-button-text')||e.target).getBoundingClientRect();
    record('click',{x:e.clientX,y:e.clientY,target:kind(e.target),hold:lastDown===null?0:performance.now()-lastDown,
      rx:r.width?(e.clientX-r.x)/r.width:0,ry:r.height?(e.clientY-r.y)/r.height:0,tw:r.width,th:r.height});
    lastDown=null;
  },true);
  addEventListener('wheel',e=>{if(e.isTrusted&&!own(e))record('wheel',{dx:e.deltaX,dy:e.deltaY,mode:e.deltaMode});},true);
  addEventListener('scroll',()=>record('scroll',{y:scrollY}),{passive:true});
  // No key value, code, text, field contents or input events leave this closure.
  // Timing is measured only in Amazon's product-search box, never other fields.
  addEventListener('keydown',e=>{
    if(!e.isTrusted||e.repeat||e.target.id!=='twotabsearchtextbox'||!allowed())return;
    const category=e.key.length===1?'character':e.key==='Backspace'?'backspace':e.key==='Enter'?'enter':'navigation';
    held.set(e.code,{start:performance.now(),flight:lastUp===null?0:performance.now()-lastUp,category});
  },true);
  addEventListener('keyup',e=>{
    const down=held.get(e.code);held.delete(e.code);
    if(!down||!e.isTrusted||e.target.id!=='twotabsearchtextbox'||!allowed())return;
    lastUp=performance.now();record('key',{hold:lastUp-down.start,flight:down.flight,category:down.category});
  },true);
  addEventListener('blur',()=>{held.clear();lastUp=null;lastDown=null;},true);
  addEventListener('pagehide',flush);
  setInterval(flush,250);
  function mount(){
    panel=document.createElement('div');panel.id='retail-demo-recorder';
    panel.style.cssText='position:fixed;bottom:16px;right:16px;z-index:2147483647';
    const root=panel.attachShadow({mode:'open'});
    root.innerHTML='<style>:host{font:13px system-ui;color:#eee}aside{background:#17191e;border:1px solid #626978;border-radius:9px;padding:12px;max-width:300px;box-shadow:0 3px 20px #0005}p{margin:0 0 8px}button{font:inherit;background:#303845;color:white;border:1px solid #667287;border-radius:5px;padding:6px 10px;margin-right:6px;cursor:pointer}small{display:block;margin-top:8px;color:#b9c3d3}</style><aside><p role="status"></p><button id="pause">Pause</button><button id="finish">Finish capture</button><small>Local capture. No typed text, passwords, screenshots or account data. Stops after 15 minutes.</small></aside>';
    label=root.querySelector('p');
    root.querySelector('#pause').onclick=e=>{flush();enabled=!enabled;commands.push(enabled?'resume':'pause');held.clear();e.target.textContent=enabled?'Pause':'Resume';render();};
    root.querySelector('#finish').onclick=()=>{flush();commands.push('finish');finished=true;held.clear();render();root.querySelectorAll('button').forEach(b=>b.disabled=true);};
    document.documentElement.append(panel);render();record('page');
  }
  if(document.readyState==='loading')addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})();
