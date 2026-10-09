
'use strict';
const actionLabel={review:'Review before purchase',automatic:'Automatic checkout',notify:'Notify only',quote:'Verify total'};
function iconButton(glyph,label,attributes=''){return `<button class="icon-button" aria-label="${esc(label)}" title="${esc(label)}" ${attributes}>${icon(glyph)}</button>`;}
function taskGroupView(load=true){
  $('#primary').hidden=true;
  if(!taskGroupId)return `<p class="help">Plans from the previous workspace remain here with their history and outcome controls. Create new groups in Task Groups.</p><section class="panel">${state.task_groups.length?table(['Plan','Status','Orders',''],state.task_groups.map(g=>`<tr><td>${esc(g.name)}</td><td>${badge(g.run?.state||'ready')}</td><td>${g.progress.confirmed_orders}</td><td><button data-tg-open="${g.id}">Review plan</button></td></tr>`)):empty('layers','No saved purchasing plans','Your original task groups are available in Task Groups.')}</section>`;
  if(load)loadTaskGroup();const g=taskGroupData;if(!g||g.id!==taskGroupId)return '<p role="status">Loading plan...</p>';
  return `<button class="back" data-tg-back>All saved plans</button><div class="tg-header"><div><h1>${esc(g.name)}</h1><p>${badge(g.run?.state||'ready')} ${g.plan.simulation?'Simulation':'Live'} ? ${g.progress.confirmed_orders} confirmed orders</p></div><div class="actions">${tgActive(g.run)||g.armed?'<button data-tg-command="stop">Stop plan</button>':''}<button data-tg-delete>Delete plan</button></div></div><p class="help">This saved plan retains its original purchase limits. Verify any uncertain outcomes below before using the same accounts in new tasks.</p><section class="panel">${tgActivity(g)}</section><details><summary>Saved products and configuration</summary>${tgProducts(g)}<pre>${esc(JSON.stringify(g.plan,null,2))}</pre></details>`;
}
