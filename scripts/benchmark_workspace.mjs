// Pure HTML generation only: no network requests, DOM layout or retailer work.
// node scripts/benchmark_workspace.mjs [checkpoint-ref]
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import vm from 'node:vm';
import {performance} from 'node:perf_hooks';
const baseline=process.argv[2]||'f09561a';
const before=execFileSync('git',['show',`${baseline}:static/task-workspace.js`],{encoding:'utf8'});
const after=readFileSync('static/task-workspace.js','utf8');
function context(source,groups,tasks){
  const state={groups,tasks,active:tasks.filter((_,i)=>i%4===0).slice(0,50).map(t=>t.id),input_lists:[]};
  const escape=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const ctx=vm.createContext({state,groupId:null,selected:new Set(),document:{addEventListener(){}},
    $:()=>({}),esc:escape,retailerName:x=>x,compactProduct:x=>x,icon:()=>'<svg></svg>',
    iconButton:()=>'<button></button>',searchField:()=>'<input>',table:(headers,rows)=>rows.join(''),empty:()=>''});
  vm.runInContext(source,ctx);return ctx;
}
function measure(ctx){
  for(let i=0;i<4;i++)vm.runInContext('taskWorkspace()',ctx);
  const times=[];let html='';
  for(let i=0;i<15;i++){const start=performance.now();html=vm.runInContext('taskWorkspace()',ctx);times.push(performance.now()-start);}
  times.sort((a,b)=>a-b);return {median_ms:+times[7].toFixed(3),p95_ms:+times[14].toFixed(3),html_bytes:Buffer.byteLength(html)};
}
const results=[];
for(const [groupCount,perGroup] of [[10,10],[100,100],[500,100]]){
  const groups=Array.from({length:groupCount},(_,i)=>({id:'g'+i,name:'Group '+i,retailer:'amazon',products:'B012345678'}));
  const tasks=groups.flatMap(g=>Array.from({length:perGroup},(_,i)=>({id:g.id+'t'+i,group_id:g.id,status:i%8===0?'completed':'idle'})));
  results.push({groups:groupCount,tasks:tasks.length,before:measure(context(before,groups,tasks)),after:measure(context(after,groups,tasks))});
}
console.log(JSON.stringify({baseline,node:process.version,scope:'Pure overview HTML generation; 4 warmups, 15 samples. Excludes browser layout, persistence and checkout.',results},null,2));
