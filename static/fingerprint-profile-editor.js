'use strict';

const generatedChoices = [
  ['cpu','Logical CPU cores',['auto','native','2','4','8','12','16']],
  ['memory','Device memory (GB)',['auto','native','8','16','32']],
  ['screen','Screen and scaling',['auto','1366x768@1','1440x900@1','1536x864@1.25','1600x900@1','1920x1080@1','2048x1152@1.25','2560x1440@1']],
  ['gpu','GPU identity',['auto','native','family-0','family-1','family-2','family-3']],
  ['fonts','Enumerated font families',['auto','native','core','office']],
  ['canvas_noise','Canvas variation',['standard','subtle']],
  ['webgl_noise','WebGL variation',['standard','subtle','off']],
  ['webgpu_limits','WebGPU capability policy',['compatible','native']]
];

function generatedFingerprintFields(account) {
  const seed = account.fingerprint_seed || (account.id ? '' : crypto.randomUUID().replaceAll('-',''));
  return `<input type="hidden" name="fingerprint_seed" value="${esc(seed)}"><p class="help">Inspect the generated values, then choose a preset for each attribute. Choices apply when the corresponding surface is enabled. GPU options stay within the actual hardware family; fonts filter installed families without changing rendering. Browser version, OS, GPU capabilities and audio output stay native.</p><div class="form-grid">${generatedChoices.map(([key,label,choices]) => select('generated_'+key,label,choices.map(value=>[value,value==='auto'?'Generated from account seed':value==='native'?'Actual browser value':value]),account.fingerprint_values?.[key]||choices[0])).join('')}</div><div class="actions"><button type="button" data-vary-gpu>Vary GPU</button><button type="button" data-preserve-gpu>Preserve actual GPU</button><button type="button" data-preview-fingerprint>Generate / refresh preview</button></div><p class="help">Preserve actual GPU selects the real GPU, turns WebGL pixel variation off, and retains native WebGPU capabilities. Other attributes and surface toggles stay as selected. Vary GPU restores generated GPU identity, standard WebGL color variation and compatible WebGPU capabilities. Aliases do not emulate another physical device; no preset guarantees a detector score.</p><p data-preview-status role="status"></p><div data-profile-preview></div>`;
}

function collectGeneratedFingerprint(form) {
  return {fingerprint_seed:form.elements.fingerprint_seed?.value || '',fingerprint_values:Object.fromEntries(generatedChoices.map(([key,,choices])=>[key,form.elements['generated_'+key]?.value||choices[0]]))};
}

function accountExtensionsFields(account) {
  const chosen = account.fingerprint_overrides?.browser_extension_ids;
  return '<h3>Extensions</h3>'+select('account_extensions_mode','Extension selection',[['global','Use global selection'],['custom','Choose for this account']],chosen == null?'global':'custom')+
    `<div data-account-extensions>${(state.browser_extensions||[]).map(extension=>`<label class="check"><input type="checkbox" data-account-extension value="${esc(extension.id)}" ${chosen?.includes(extension.id)?'checked':''}>${esc(extension.name)}</label>`).join('')||'<p class="help">Add extensions in Settings first.</p>'}</div><p class="help">Selected extensions are also allowed in incognito. New accounts receive a saved random selection from eligible extensions when enabled in Settings.</p>`;
}

function browserProfileSettings(settings) {
  return check('browser_incognito','Launch in incognito',settings.browser_incognito ?? true)+
    select('browser_identity','Browser identity',[['default','Use browser application'],['chrome','Google Chrome'],['msedge','Microsoft Edge'],['brave','Brave'],['opera','Opera']],settings.browser_identity||'default')+
    '<p class="help">Edge, Brave and Opera launch the actual installed application in JavaScript mode. Native mode uses the dedicated Chromium fork. Normal profiles retain account browser data on this device.</p>'+
    input('brave_executable','Brave executable (optional)',settings.brave_executable||'')+input('opera_executable','Opera executable (optional)',settings.opera_executable||'')+
    '<h4>Global extensions</h4>'+(state.browser_extensions||[]).map(extension=>`<label class="check"><input type="checkbox" data-global-extension value="${esc(extension.id)}" ${settings.browser_extension_ids?.includes(extension.id)?'checked':''}>${esc(extension.name)}</label>`).join('')+
    check('random_account_extensions','Randomly assign eligible extensions to new accounts',settings.random_account_extensions ?? true)+
    '<button type="button" data-manage-browser-extensions>Manage extensions</button><p class="help">Extensions load in the selected account browser. Extension assignments are saved once and stay stable between launches.</p>';
}

function manageBrowserExtensions() {
  const d=dialog(`<form><h2>Browser extensions</h2><p class="help">Add an unpacked extension folder. Use the publishers below to obtain common extensions, including Rakuten.</p><div class="actions">${(state.extension_catalog||[]).map(item=>`<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.name)}</a><button type="button" data-install-extension="${esc(item.id)}">Install</button>`).join('')}</div><div data-extension-list>${(state.browser_extensions||[]).map(item=>`<p>${esc(item.name)} <button type="button" data-delete-extension="${esc(item.id)}">Remove</button></p>`).join('')}</div>${input('name','Extension name','','text','required')}${input('path','Unpacked extension folder','','text','required')}${check('random_eligible','Include in random assignments for new accounts',true)}<p data-extension-error role="alert"></p><div class="modal-actions"><button type="button" data-close-extensions>Close</button><button type="submit">Add extension</button></div></form>`);
  d.querySelectorAll('[data-install-extension]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{await api('browser-extensions/install/'+button.dataset.installExtension,'POST');await refresh();d.close();manageBrowserExtensions();}catch(error){d.querySelector('[data-extension-error]').textContent=error.message;}finally{button.disabled=false;}});
  d.querySelector('[data-close-extensions]').onclick=()=>d.close();
  d.querySelectorAll('[data-delete-extension]').forEach(button=>button.onclick=async()=>{try {await api('browser_extensions/'+button.dataset.deleteExtension,'DELETE');await refresh();button.closest('p').remove();}catch(error){d.querySelector('[data-extension-error]').textContent=error.message;}});
  d.querySelector('form').onsubmit=async event=>{event.preventDefault();try{await api('browser_extensions','POST',formData(event.target));await refresh();d.close();toast('Extension added');}catch(error){d.querySelector('[data-extension-error]').textContent=error.message;}};
}

async function previewFingerprint(root, id) {
  const form=root.closest('form'), status=root.querySelector('[data-preview-status]');
  if ((form.elements.account_fingerprint_backend?.value || state.settings[0].fingerprint_backend) === 'fingerprint-suite') {
    status.textContent='Fingerprint-suite manages its complete profile. Select JavaScript compatibility or Native Chromium to edit these presets.';
    root.querySelector('[data-profile-preview]').innerHTML='';
    return;
  }
  status.textContent='Reading browser hardware and generating compatible values…';
  try {
    const preview=await api('fingerprints/preview','POST',{id,region:form.elements.region?.value||root.closest('dialog').dataset.accountRegion||'US',fingerprint_overrides:collectAccountFingerprint(form),...collectGeneratedFingerprint(form)});
    const gpu=form.elements.generated_gpu, old=gpu.value;
    gpu.innerHTML='<option value="auto">Generated from account seed</option>'+preview.gpu_choices.map(([key,label])=>`<option value="${esc(key)}">${esc(label)}</option>`).join('');gpu.value=[...gpu.options].some(option=>option.value===old)?old:'auto';
    for(const key of ['cpu','memory'])for(const option of form.elements['generated_'+key].options)option.disabled=!isNaN(Number(option.value))&&Number(option.value)>preview.hardware[key];
    const values=preview.generated;
    root.querySelector('[data-profile-preview]').innerHTML=table(['Attribute','Generated value'],Object.entries(values).map(([key,value])=>`<tr><td>${esc(key.replaceAll('_',' '))}</td><td class="fingerprint-value">${esc(Array.isArray(value)?value.join(', '):typeof value==='object'?JSON.stringify(value):value)}</td></tr>`))+`<details><summary>Native browser information</summary><pre class="fingerprint-value">${esc(JSON.stringify(preview.hardware,null,2))}</pre></details>`;
    status.textContent='Compatible presets generated. Enable their surface controls to apply them.'+(preview.settings.fingerprint_backend==='native'?' Native Chromium retains native WebGPU limits; the capability-policy choice applies to JavaScript mode.':'');
  } catch(error){status.textContent=error.message;}
}

document.addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.fingerprintTab){const root=button.closest('.account-fingerprint-fields');root.querySelectorAll('[data-fingerprint-pane]').forEach(pane=>pane.hidden=pane.dataset.fingerprintPane!==button.dataset.fingerprintTab);root.querySelectorAll('[data-fingerprint-tab]').forEach(tab=>tab.setAttribute('aria-selected',String(tab===button)));if(button.dataset.fingerprintTab==='generated')previewFingerprint(root,root.closest('dialog').dataset.accountId || (editing?.kind==='accounts'?editing.id:undefined));}
  if(button.hasAttribute('data-preserve-gpu')||button.hasAttribute('data-vary-gpu')){const root=button.closest('.account-fingerprint-fields'),form=root.closest('form');for(const [key,value] of Object.entries(button.hasAttribute('data-vary-gpu')?{gpu:'auto',webgl_noise:'standard',webgpu_limits:'compatible'}:{gpu:'native',webgl_noise:'off',webgpu_limits:'native'}))form.elements['generated_'+key].value=value;previewFingerprint(root,root.closest('dialog').dataset.accountId || (editing?.kind==='accounts'?editing.id:undefined));}
  if(button.hasAttribute('data-preview-fingerprint'))previewFingerprint(button.closest('.account-fingerprint-fields'),button.closest('dialog').dataset.accountId || (editing?.kind==='accounts'?editing.id:undefined));
  if(button.hasAttribute('data-manage-browser-extensions'))manageBrowserExtensions();
  if(button.dataset.accountMore){const id=button.dataset.accountMore;const d=dialog(`<h2>Account actions</h2><div class="actions"><button data-link-account="${id}">Linked profiles</button><button data-action="edit" data-kind="accounts" data-id="${id}">Edit account</button><button data-action="delete" data-kind="accounts" data-id="${id}">Delete account</button><button data-dismiss-menu>Cancel</button></div>`);d.addEventListener('click',e=>{if(e.target.closest('button'))d.close();});}
});
