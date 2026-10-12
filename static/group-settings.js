'use strict';
let taskGroupSettingsTab='general';
function groupSettings(g,active=false){const amazon=g.retailer==='amazon';return `<div class="group-nav">${tabs([['general','General'],['monitoring','Monitoring'],['checkout','Checkout'],['advanced','Advanced']],taskGroupSettingsTab,'data-work-tab')}</div><form data-work-settings data-preserve-draft="${g.id}">${pane('general',taskGroupSettingsTab,input('name','Group name',g.name,'text','required maxlength="100"')+`<label>Site</label><p>${esc(retailerName(g.retailer))}</p>`)}${pane('monitoring',taskGroupSettingsTab,`<label for="monitor-products">Monitor Input</label><textarea id="monitor-products" name="products">${esc(g.products)}</textarea>`+select('input_list_id','Input List',[['','Use monitor input'],...state.input_lists.filter(x=>x.retailer===g.retailer).map(x=>[x.id,x.name])],g.input_list_id||'')+select('monitor_proxy_id','Monitor Proxy Group',[['','Direct monitor connection'],...state.proxies.map(x=>[x.id,x.name])],g.monitor_proxy_id||'')+input('delay_ms','Monitor Delay',g.delay_ms??4500,'number','min="3500" required')+input('min_price','Price Minimum',g.min_price??0,'number','min="0" step="0.01"')+input('max_price','Price Maximum',g.max_price??'','number','min="0" step="0.01"')+(amazon?offerSettings(g)+check('allow_third_party','Allow Third-Party Sellers',g.allow_third_party)+check('allow_used','Allow Used Products',g.allow_used)+check('skip_monitoring','Prefer saved offer (stock is still verified)',g.skip_monitoring)+'<p class="help">Requires an ASIN and offer ID. Product and price are still verified before carting.</p>':'')+`<div id="monitor-statuses">${monitorStatuses(g)}</div>`)}${pane('checkout',taskGroupSettingsTab,input('quantity','Default Item Quantity',g.quantity??1,'number','min="1" max="30"')+input('max_total','Maximum Order Total',g.max_total??100,'number','min="0" step="0.01"')+input('max_errors','Maximum Checkout Errors',g.max_errors??5,'number','min="1" max="30"')+input('retry_delay_ms','Default Retry Delay',g.retry_delay_ms??3500,'number','min="1000"')+check('loop','Loop Checkouts',g.loop)+input('max_checkouts','Maximum Checkouts Per Run',g.max_checkouts??1,'number','min="1" max="100"')+'<p class="help">Looping only continues after a verified success. Uncertain order results stop for review.</p>')}${pane('advanced',taskGroupSettingsTab,select('mode','Monitor mode',[['restock','Restocks'],['deals','Deals']],g.mode)+check('only_freebies','Only Freebies',g.only_freebies)+input('min_discount','Minimum Discount (%)',g.min_discount??0,'number','min="0" max="100"')+input('min_savings','Minimum Savings',g.min_savings??0,'number','min="0" step="0.01"')+check('notify_offer','Notify when Offer ID Found',g.notify_offer)+input('monitor_concurrency','Concurrent product checks',g.monitor_concurrency??3,'number','min="1" max="10"')+input('highlight','Group highlight',g.highlight||'#64d9ad','color'))}<p data-work-error role="alert"></p><button class="primary save-group" type="submit" ${active?'disabled':''}>Save settings</button></form>`;}

const offerSettingHelp = {
  offer_id: 'Leave blank to discover the active offer automatically.',
  use_offer_id: 'When enabled, a supplied ID is an exact-match filter; discovered IDs are used only for the current run. When disabled, saved IDs are kept but ignored and normal Add to Cart is used. Discovery remains visible in Product observations.',
  offer_id_shortcut: 'Direct Offer ID carting submits a supported Add to Cart form once without mouse movement or click timing. It requires Use Offer ID; unsupported forms use a normal click. Account, stock and price checks remain. Buy Now uses its normal path.',
  offer_id_endpoint: 'Send directly to endpoint also requires Direct Offer ID carting. It sends one request without loading the Add to Cart response page, then checks the cart. Initial account and product-page discovery still run; this does not enable endpoint polling. Account, stock and price checks remain. Buy Now uses its normal path.'
};
function offerHelpButton(key, label) {
  const id=`offer-help-${key}`;
  return `<button type="button" class="setting-info" data-offer-help aria-label="About ${esc(label)}" aria-describedby="${id}">${icon('info',15)}</button><div id="${id}" class="setting-tooltip" role="tooltip" popover="manual">${esc(offerSettingHelp[key])}</div>`;
}
function offerSettings(g) {
  const field=input('offer_id','Offer ID',g.offer_id||'');
  const boundary=field.indexOf('</label>')+8;
  return `<div class="setting-help-row">${field.slice(0,boundary)}${offerHelpButton('offer_id','Offer ID')}</div>${field.slice(boundary)}`+
    [['use_offer_id','Use Offer ID when found',g.use_offer_id??true],
     ['offer_id_shortcut','Direct Offer ID carting',g.offer_id_shortcut??false],
     ['offer_id_endpoint','Send directly to endpoint',g.offer_id_endpoint??false]]
      .map(([key,label,value])=>`<div class="setting-help-row">${check(key,label,value)}${offerHelpButton(key,label)}</div>`).join('');
}
// Delegated handlers survive workspace refreshes. Popovers use the top layer so
// the independently scrolling group sidebar cannot clip the explanation.
let openOfferHelp=null, offerHelpTimer;
function closeOfferHelp() {
  clearTimeout(offerHelpTimer);
  if(openOfferHelp?.tip.isConnected && openOfferHelp.tip.matches(':popover-open'))openOfferHelp.tip.hidePopover();
  openOfferHelp=null;
}
function showOfferHelp(button) {
  clearTimeout(offerHelpTimer);
  if(openOfferHelp?.button===button)return;
  closeOfferHelp();
  const tip=document.getElementById(button.getAttribute('aria-describedby'));
  if(!tip)return;
  openOfferHelp={button,tip,pinned:false};tip.showPopover();
  positionOfferHelp();
}
function positionOfferHelp() {
  if(!openOfferHelp)return;
  const {button,tip}=openOfferHelp;
  if(!button.isConnected){closeOfferHelp();return;}
  const rect=button.getBoundingClientRect(), box=tip.getBoundingClientRect(), gap=6, edge=12;
  const sidebar=button.closest('.task-sidebar')?.getBoundingClientRect();
  if(rect.bottom<Math.max(0,sidebar?.top??0)||rect.top>Math.min(innerHeight,sidebar?.bottom??innerHeight)){closeOfferHelp();return;}
  tip.style.left=`${Math.max(edge,Math.min(rect.left,innerWidth-box.width-edge))}px`;
  const below=rect.bottom+gap;
  tip.style.top=`${Math.max(edge,Math.min(below+box.height<=innerHeight-edge?below:rect.top-box.height-gap,innerHeight-box.height-edge))}px`;
}
function withinOfferHelp(node) {
  return node instanceof Node && openOfferHelp && (openOfferHelp.button.contains(node)||openOfferHelp.tip.contains(node));
}
document.addEventListener('pointerover',event=>{
  if(event.pointerType==='touch')return;
  const button=event.target.closest('[data-offer-help]');
  if(button)showOfferHelp(button);
  else if(withinOfferHelp(event.target))clearTimeout(offerHelpTimer);
});
document.addEventListener('pointerout',event=>{
  if(!withinOfferHelp(event.target)||withinOfferHelp(event.relatedTarget))return;
  offerHelpTimer=setTimeout(()=>{if(openOfferHelp&&!openOfferHelp.pinned&&document.activeElement!==openOfferHelp.button)closeOfferHelp();},120);
});
document.addEventListener('focusin',event=>{
  const button=event.target.closest('[data-offer-help]');if(button)showOfferHelp(button);
});
document.addEventListener('focusout',event=>{
  if(openOfferHelp?.button===event.target&&!withinOfferHelp(event.relatedTarget))closeOfferHelp();
});
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-offer-help]');if(!button)return;
  if(openOfferHelp?.button===button&&openOfferHelp.pinned)closeOfferHelp();
  else{showOfferHelp(button);openOfferHelp.pinned=true;}
});
document.addEventListener('pointerdown',event=>{if(openOfferHelp&&!withinOfferHelp(event.target))closeOfferHelp();});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&openOfferHelp){closeOfferHelp();event.preventDefault();}});
window.addEventListener('resize',closeOfferHelp);
window.addEventListener('scroll',event=>{if(!openOfferHelp?.tip.contains(event.target instanceof Node?event.target:null))positionOfferHelp();},true);
