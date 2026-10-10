'use strict';
// Presentation groups only; views and API records remain the source of truth.
const navigationSections = [
  {primary:'accounts', items:[['accounts','Accounts'],['manager','Sessions & relationships']]},
  {primary:'checkouts', items:[['checkouts','Orders'],['monitors','Monitors'],['events','Activity'],['feed','Product observations'],['saved_plans','Saved plans']]},
  {primary:'tools', items:[['tools','Overview'],['input_lists','Product lists'],['mailboxes','Mailboxes & codes'],['solvers','Verification'],['troubleshooting','Diagnostics'],['retailers','Retailers']]}
];
Object.assign(titles,{monitors:['Monitors','Shared stock checks and the tasks waiting for them.','']});
function renderSectionNavigation(){
  const section=navigationSections.find(s=>s.items.some(([id])=>id===view)), primary=section?.primary||view;
  document.querySelectorAll('.sidebar [data-view]').forEach(button=>{
    const active=button.dataset.view===primary;
    button.classList.toggle('active',active);
    if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
  });
  updateRegion($('#section-tabs'),section?tabs(section.items,view,'data-context-view'):'');
}
function searchField(key,label,value=''){
  return `<label class="search-field">${icon('search',16)}<input type="search" data-live-search="${key}" aria-label="${esc(label)}" placeholder="${esc(label)}" value="${esc(value)}"></label>`;
}
function monitorWorkspace(group=null){
  const records=(state.monitors||[]).filter(m=>!group||m.group_id===group.id);
  return `<section class="panel"><div class="toolbar"><h2>Stock monitors</h2><span class="spacer"></span><span class="muted">${records.filter(m=>m.task_ids.length).length} active</span></div>${records.length?table(['Product / group','Status','Waiting tasks','Connection','Activity'],records.map(m=>`<tr data-monitor-row="${esc(m.id)}"><td><strong class="mono">${esc(m.asin)}</strong><small>${esc(name('groups',m.group_id))}</small></td><td>${badge(m.status)}<small>${esc(m.message)}</small></td><td>${m.task_ids.length}</td><td>${esc(m.simulation?'Simulation':m.region)}<small>${esc(m.connection)}</small></td><td><button data-monitor-log="${esc(m.id)}">View log</button></td></tr>`)):empty('activity','No monitor activity','Start a task to begin checking its assigned products. Shared checks do not hold an account checkout slot.')}</section>`;
}
document.addEventListener('input',e=>{
  const key=e.target.dataset.liveSearch;if(!key)return;
  if(key==='groups')groupQuery=e.target.value;
  if(key==='tasks'){taskQuery=e.target.value;selected.clear();}
  if(key==='resources')resourceQueries[view]=e.target.value;
  render();
});
