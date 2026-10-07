'use strict';

// Drafts stay in memory, including across navigation. Never persist secret fields
// in localStorage or include them in unrelated connection requests.
const settingsSections = [['general','General'], ['browser','Browser'], ['notifications','Notifications'], ['integrations','Connections'], ['data','Data & privacy']];
const stoppedSettings = ['max_running_tasks','browser_channel','show_browser_window','cdp_attach','cdp_endpoint','fingerprint_backend','native_browser_executable','agent_mode','ai_connection_id'];
let settingsDraft = null, settingsBaseline = null, settingsSaving = false, settingsError = '', settingsSavedMessage = '';
let installedBrowsers = null, browserLookupPending = false;
Object.assign(titles, {
  settings: ['Settings', 'Manage your workspace. Changes apply only when you save.', ''],
  troubleshooting: ['Troubleshooting', 'Connection checks, activity and diagnostic evidence.', ''],
  retailers: ['Retailer status', 'Available integrations and their implementation status.', '']
});

function savedSettings() {
  return {...(state.settings[0] || {}), webhook: '', webhook_action: 'keep'};
}
function ensureSettingsDraft() {
  if (!settingsDraft || (!settingsSaving && !settingsDirty())) {
    settingsBaseline = savedSettings();
    settingsDraft = {...settingsBaseline};
  }
}
function settingsChanges() {
  if (!settingsDraft) return {};
  const changes = {};
  for (const [key, value] of Object.entries(settingsDraft)) {
    if (['id','has_webhook','webhook','webhook_action'].includes(key)) continue;
    if (value !== settingsBaseline[key]) changes[key] = value;
  }
  if (settingsDraft.webhook_action === 'remove') changes.webhook = '';
  if (settingsDraft.webhook_action === 'replace') changes.webhook = settingsDraft.webhook.trim();
  return changes;
}
function settingsDirty() { return Object.keys(settingsChanges()).length > 0; }
function settingHelp(text) { return `<p class="setting-help">${text}</p>`; }
function settingCard(title, help, content) {
  return `<div class="setting-card"><h3>${title}</h3>${help ? settingHelp(help) : ''}${content}</div>`;
}
function secondsField(key, label, value, min, max) {
  return input(key, label, value === '' ? '' : value / 1000, 'number', `data-milliseconds min="${min}" max="${max}" step="0.1" required`);
}
function settingsPanel(key, html) {
  return `<section id="settings-panel-${key}" role="tabpanel" aria-labelledby="settings-tab-${key}" data-pane="${key}" ${settingsTab === key ? '' : 'hidden'}><h2>${settingsSections.find(x => x[0] === key)[1]}</h2>${html}</section>`;
}
function settingsView() {
  if (!state.settings.length) return '<p role="status">Loading settings…</p>';
  ensureSettingsDraft();
  if (!settingsSections.some(x => x[0] === settingsTab)) settingsTab = 'general';
  const s = settingsDraft;
  const general = settingCard('Monitoring defaults', 'Applies to new task groups. Existing groups keep their own interval.',
    secondsField('default_monitor_delay', 'Check interval (seconds)', s.default_monitor_delay ?? 4500, 3.5, 3600)) +
    settingCard('Resource usage', 'Extra tasks queue when all browser workers are busy. Higher limits use more memory.',
      input('max_running_tasks', 'Maximum active browser workers', s.max_running_tasks ?? 10, 'number', 'min="1" max="50" required') +
      '<p class="setting-help" data-running-note></p><details><summary>Advanced network limits</summary>' +
      input('proxy_timeout_seconds', 'Proxy check timeout (seconds)', s.proxy_timeout_seconds ?? 15, 'number', 'min="3" max="60" required') +
      input('proxy_concurrency', 'Concurrent proxy checks', s.proxy_concurrency ?? 5, 'number', 'min="1" max="20" required') + '</details>') +
    settingCard('Application status', '', '<button type="button" data-context-view="retailers">View retailer availability</button>');
  const browserOptions = (installedBrowsers || [['chromium','Bundled Chromium'],['chrome','Chrome'],['msedge','Edge']].map(([id,label]) => ({id,label,available:null})))
    .map(b => [b.id, b.label + (b.available === false ? ' — not installed' : b.available === true ? ' — installed' : '')]);
  const browser = settingCard('Browser session', 'Applies when a new browser session opens. Stop running tasks before changing the connection or window mode.',
    select('cdp_attach', 'Browser connection', [['false','Managed by Retail Desk'],['true','Existing browser (advanced)']], String(s.cdp_attach ?? false)) +
    '<div data-managed-browser>' +
    select('show_browser_window', 'Window mode', [['true','Visible browser (default)'],['false','Headless — no browser window']], String(s.show_browser_window ?? true)) +
    settingHelp('Visible mode lets you see and interact with the browser. Headless mode uses no desktop window; View live remains available.') +
    select('browser_channel', 'Browser application', browserOptions, s.browser_channel || 'chromium') +
    '<p class="setting-help" data-browser-availability>Checking installed browsers…</p></div>' +
    '<div data-existing-browser hidden>' + input('cdp_endpoint', 'Local debugging address', s.cdp_endpoint || 'http://127.0.0.1:9222', 'url') +
    settingHelp('Start a dedicated Chrome or Edge debugging session first. Its window mode is controlled outside this app.') +
    '<button type="button" data-inspect-cdp>Test saved connection</button>' + settingHelp('Save an address change before testing it.') + '</div>') +
    settingCard('Page loading', '', secondsField('browser_timeout_ms', 'Page timeout (seconds)', s.browser_timeout_ms ?? 30000, 5, 120)) +
    '<details class="setting-card"><summary>Advanced browser options</summary>' +
    select('interaction_pacing', 'Input timing', [['off','Standard input'],['paced','Paced input (slower)']], s.interaction_pacing || 'off') +
    settingHelp('Applies to new browser sessions.') + legacyProfileSettings(s) + '</details>';
  const notifications = settingCard('App sounds', 'Keep the dashboard open to hear sounds. Preview also enables browser audio.',
    check('checkout_sound', 'Play a sound for completed checkouts', s.checkout_sound ?? true) +
    check('attention_sound', 'Play a sound when attention is needed', s.attention_sound ?? true) +
    select('sound_style', 'Checkout sound', [['chime','Chime'],['bell','Bell'],['pulse','Pulse']], s.sound_style || 'chime') +
    input('sound_volume', 'Volume', s.sound_volume ?? 0.4, 'range', 'min="0" max="1" step="0.05" aria-describedby="settings-volume"') +
    '<output id="settings-volume" for="f-sound_volume"></output><div class="setting-actions"><button type="button" data-feature="sound-test">Preview sound</button></div>') +
    settingCard('Discord', 'Notification preferences take effect after saving.',
    check('notifications', 'Send Discord notifications', s.notifications ?? false) +
    `<p class="setting-help">Webhook: ${s.has_webhook ? 'saved securely' : 'not configured'}</p>` +
    select('webhook_action', 'Webhook', [['keep',s.has_webhook ? 'Keep saved webhook' : 'Not configured'],['replace',s.has_webhook ? 'Replace webhook' : 'Add webhook'],...(s.has_webhook ? [['remove','Remove saved webhook']] : [])], s.webhook_action) +
    '<div data-webhook-entry hidden>' + input('webhook','Discord webhook URL', s.webhook, 'password', 'autocomplete="new-password"') + '</div>' +
    '<div data-discord-events>' + check('webhook_checkouts','Checkout confirmations', s.webhook_checkouts ?? true) +
    check('webhook_attention','Requests for attention', s.webhook_attention ?? true) + '</div>' +
    '<button type="button" data-test-discord>Send test to saved webhook</button>' + settingHelp('Sends one test message immediately. Save webhook changes first.') +
    '<p data-discord-result role="status"></p>');
  const connections = settingCard('AI assistance', '1. Add credentials. 2. Test the connection. 3. Choose when assistance is allowed and save.',
    '<div data-ai-readiness></div>' +
    select('ai_connection_id','Connection', [['','Choose a connection'],...(state.ai_connections || []).map(c => [c.id,c.name + ' · ' + c.model])], s.ai_connection_id || '') +
    select('agent_mode','When AI may assist', [['off','Off'],['recovery','When normal navigation fails'],['agent','At every cart and checkout step']], s.agent_mode || 'off') +
    settingHelp('Adding a key does not enable assistance. Provider charges may apply. Item, seller and final-price checks remain required.') +
    '<button type="button" data-ai-add>Add API connection</button><p class="setting-help">Connection edits save separately. Your settings draft is kept.</p><div class="ai-connections">' +
    (state.ai_connections || []).map(c => `<article class="connection-card"><h4>${esc(c.name)}</h4><p>${esc(c.model)} · ${c.has_api_key ? 'Key saved' : 'Key missing'}</p><p>${esc(c.health?.message || 'Not tested')}</p><div class="setting-actions"><button type="button" data-ai-edit="${esc(c.id)}">Edit</button><button type="button" data-ai-test="${esc(c.id)}">Test connection</button><button type="button" data-ai-delete="${esc(c.id)}" ${(state.settings[0]?.ai_connection_id === c.id || s.ai_connection_id === c.id) ? 'disabled title="Deselect this connection and save before deleting"' : ''}>Delete</button></div></article>`).join('') + '</div>' +
    '<details><summary>Advanced AI limits</summary>' + input('agent_max_steps','Maximum model calls per action',s.agent_max_steps ?? 4,'number','min="1" max="8" required') +
    input('agent_timeout_seconds','AI action timeout (seconds)',s.agent_timeout_seconds ?? 60,'number','min="10" max="180" required') +
    input('diagnosis_endpoint','Legacy local diagnosis address (optional)',s.diagnosis_endpoint || '', 'url') + '</details>') +
    settingCard('Other connections', '', '<div class="setting-actions"><button type="button" data-context-view="mailboxes">Email & verification codes</button><button type="button" data-context-view="solvers">Solver connections</button></div>');
  const data = settingCard('Workspace backup', 'Includes the encrypted workspace database and vault key. On Windows, restore requires the same Windows account that created the backup. Browser installation files are not included.',
    '<a class="setting-link" href="/api/data/backup">Download encrypted backup</a><details><summary>How to restore a backup</summary><ol><li>Stop tasks and close Retail Desk.</li><li>Keep a copy of your current data folder.</li><li>Extract the backup into a new, empty folder. Keep retail.sqlite3 and vault.key together.</li><li>Start Retail Desk with <code>python run.py --data-dir "path-to-restored-folder"</code> using its virtual environment and the same Windows account.</li></ol><p class="setting-help">Restoring scheduled groups can resume their scheduled work. Review the backup before using it as your active workspace.</p></details>') +
    settingCard('Import records', 'Imports add records to this workspace. They do not restore a workspace backup. Order exports are in order history.',
      '<div class="setting-actions"><button type="button" data-feature="import" data-kind="accounts">Import accounts</button><button type="button" data-feature="import" data-kind="profiles">Import profiles</button><button type="button" data-context-view="checkouts">Open order history</button></div>') +
    settingCard('Privacy & diagnostic recording', 'Credentials, sessions and diagnostic evidence are encrypted locally. Saved secrets are not returned to the dashboard.',
      check('trace_enabled','Record browser traces for troubleshooting',s.trace_enabled ?? false) +
      '<p data-trace-status role="status"></p>' + settingHelp('Traces can contain account details. Keep recording off unless you need to investigate a problem.'));
  const nav = settingsSections.map(([key,label]) => `<button type="button" id="settings-tab-${key}" role="tab" aria-controls="settings-panel-${key}" aria-selected="${settingsTab === key}" tabindex="${settingsTab === key ? 0 : -1}" data-settings-tab="${key}" class="${settingsTab === key ? 'selected' : ''}">${label}</button>`).join('');
  const html = `<div data-settings-shell class="settings-workspace"><div class="settings-navigation"><div role="tablist" aria-label="Settings sections" aria-orientation="vertical">${nav}</div><button type="button" data-context-view="troubleshooting">Troubleshooting ↗</button></div><form id="settings-form" novalidate><div class="settings-panels">${settingsPanel('general',general)}${settingsPanel('browser',browser)}${settingsPanel('notifications',notifications)}${settingsPanel('integrations',connections)}${settingsPanel('data',data)}</div><div class="settings-savebar"><p id="settings-error" role="alert" tabindex="-1">${esc(settingsError)}</p><div class="setting-actions"><span data-settings-status role="status"></span><button type="button" data-settings-discard>Discard changes</button><button class="primary" type="submit">Save changes</button></div></div></form></div>`;
  queueMicrotask(() => { updateSettingsStatus(); loadInstalledBrowsers(); });
  return html;
}

function captureSettingsDraft() {
  const form = $('#settings-form');
  if (!form || settingsSaving) return;
  ensureSettingsDraft();
  for (const el of form.elements) {
    if (!el.name) continue;
    let value = el.type === 'checkbox' ? el.checked : el.value;
    if (el.type === 'number' || el.type === 'range') value = value === '' ? '' : Number(value);
    if (el.hasAttribute('data-milliseconds') && value !== '') value = Math.round(value * 1000);
    if (['show_browser_window','cdp_attach'].includes(el.name)) value = value === 'true';
    settingsDraft[el.name] = value;
  }
}
function refreshSettingsView() {
  if (view !== 'settings' && view !== 'troubleshooting') return;
  captureSettingsDraft();
  if (view === 'troubleshooting') { $('#screen').innerHTML = troubleshootingView(); return; }
  if (!settingsDirty()) { settingsDraft = null; ensureSettingsDraft(); }
  const openDetails = [...document.querySelectorAll('#settings-form details')].map(d => d.open);
  $('#screen').innerHTML = settingsView();
  document.querySelectorAll('#settings-form details').forEach((d,i) => { d.open = openDetails[i] || false; });
  updateSettingsStatus();
}
function activateSettingsTab(key, focus = false) {
  if (!settingsSections.some(x => x[0] === key)) return;
  settingsTab = key;
  setPane($('#settings-form'), key);
  document.querySelectorAll('[data-settings-tab]').forEach(b => {
    const active = b.dataset.settingsTab === key;
    b.classList.toggle('selected',active); b.setAttribute('aria-selected',String(active)); b.tabIndex = active ? 0 : -1;
    if (active && focus) b.focus();
  });
}
function updateSettingsStatus() {
  const form = $('#settings-form');
  if (!form || !settingsDraft) return;
  const s = settingsDraft, saved = state.settings[0] || {}, running = !!state.active?.length;
  const dirty = settingsDirty();
  document.querySelector('.settings-navigation [role=tablist]').setAttribute('aria-orientation', matchMedia('(max-width:740px)').matches ? 'horizontal' : 'vertical');
  form.querySelector('[data-settings-status]').textContent = settingsSaving ? 'Saving…' : dirty ? 'Unsaved changes · applies across sections' : (settingsSavedMessage || 'All changes saved');
  form.querySelector('[type=submit]').disabled = settingsSaving || !dirty;
  form.querySelector('[data-settings-discard]').disabled = settingsSaving || !dirty;
  form.querySelector('[data-running-note]').textContent = running ? 'Running tasks: worker and connection settings are locked until tasks stop.' : 'Changes to the worker limit require stopped tasks.';
  syncFingerprintBackendFields(form);
  const nativePath = form.elements.native_browser_executable;
  if (nativePath) {
    const visible = s.fingerprint_backend === 'native';
    nativePath.hidden = !visible;
    for (const label of nativePath.labels) label.hidden = !visible;
  }
  for (const key of stoppedSettings) if (form.elements[key]) {
    form.elements[key].disabled = running || settingsSaving;
    form.elements[key].title = running ? 'Stop running tasks to change this setting' : '';
  }
  form.querySelector('[data-managed-browser]').hidden = !!s.cdp_attach;
  form.querySelector('[data-existing-browser]').hidden = !s.cdp_attach;
  form.querySelector('[data-webhook-entry]').hidden = s.webhook_action !== 'replace';
  form.querySelector('[data-discord-events]').hidden = !s.notifications;
  const webhookTest = form.querySelector('[data-test-discord]');
  webhookTest.disabled = !saved.has_webhook || s.webhook_action !== 'keep';
  form.querySelector('#settings-volume').textContent = `${Math.round((s.sound_volume ?? .4) * 100)}%`;
  form.querySelector('[data-trace-status]').textContent = saved.trace_enabled ? 'Trace recording is enabled in saved settings.' : 'Trace recording is off in saved settings.';
  const connection = (state.ai_connections || []).find(c => c.id === s.ai_connection_id);
  const ready = connection?.has_api_key && connection?.health?.ok;
  form.querySelector('[data-ai-readiness]').textContent = `Assistance: ${(saved.agent_mode || 'off') === 'off' ? 'Off' : 'Enabled'} (saved). Selected connection: ${!connection ? 'not selected' : ready ? 'Ready' : 'Needs attention — save credentials and test the connection'}.`;
  const browser = installedBrowsers?.find(b => b.id === s.browser_channel);
  for (const option of form.elements.browser_channel.options) {
    const item = installedBrowsers?.find(b => b.id === option.value);
    if (item) option.disabled = !item.available;
  }
  form.querySelector('[data-browser-availability]').textContent = browser ? (browser.available ? 'Installed and available for new sessions.' : 'Not found. Install this browser or select an installed option.') : 'Browser availability has not been checked.';
  if (settingsSaving) for (const el of form.elements) el.disabled = true;
}
async function loadInstalledBrowsers() {
  if (installedBrowsers || browserLookupPending) return;
  browserLookupPending = true;
  try {
    installedBrowsers = await api('settings/browser-options');
    if (view === 'settings') {
      const selectEl = $('#settings-form')?.elements.browser_channel;
      if (selectEl) for (const option of selectEl.options) {
        const item = installedBrowsers.find(b => b.id === option.value);
        if (item) { option.textContent = item.label + (item.available ? ' — installed' : ' — not installed'); option.disabled = !item.available; }
      }
      updateSettingsStatus();
    }
  } catch { /* Keep manual choices usable when discovery is unavailable. */ }
  finally { browserLookupPending = false; }
}
function settingsFieldError(message, field) {
  settingsError = message;
  const error = $('#settings-error'); error.textContent = message;
  if (field) {
    const panel = field.closest('[data-pane]'); if (panel) activateSettingsTab(panel.dataset.pane);
    for (let p = field.parentElement; p && p !== $('#settings-form'); p = p.parentElement) if (p.tagName === 'DETAILS') p.open = true;
    field.setAttribute('aria-invalid','true'); field.setAttribute('aria-describedby','settings-error');
    if (!field.disabled) field.focus(); else error.focus();
  } else error.focus();
}
async function saveSettings(form) {
  if (settingsSaving) return;
  captureSettingsDraft();
  for (const field of form.elements) {
    field.removeAttribute('aria-invalid');
    if (field.willValidate && !field.checkValidity()) return settingsFieldError(`${field.labels?.[0]?.textContent || field.name}: ${field.validationMessage}`, field);
  }
  const patch = settingsChanges();
  if (!Object.keys(patch).length) return;
  if (state.active?.length) {
    const locked = stoppedSettings.find(key => key in patch);
    if (locked) return settingsFieldError('Stop running tasks before saving this change. Your draft is kept.',form.elements[locked]);
  }
  if (settingsDraft.notifications && !(settingsDraft.webhook_action === 'replace' ? settingsDraft.webhook.trim() : settingsDraft.webhook_action !== 'remove' && settingsDraft.has_webhook))
    return settingsFieldError('Add a Discord webhook or turn off Discord notifications.',form.elements.webhook_action);
  if (settingsDraft.webhook_action === 'replace' && !/^https:\/\/(discord\.com|discordapp\.com)\/api\/webhooks\/\d+\/[A-Za-z0-9_-]+$/.test(settingsDraft.webhook.trim()))
    return settingsFieldError('Enter a valid Discord webhook URL.',form.elements.webhook);
  if (settingsDraft.agent_mode !== 'off' && !settingsDraft.ai_connection_id)
    return settingsFieldError('Select an API connection before enabling assistance.',form.elements.ai_connection_id);
  const selectedBrowser = installedBrowsers?.find(b => b.id === settingsDraft.browser_channel);
  if (!settingsDraft.cdp_attach && selectedBrowser?.available === false && ('browser_channel' in patch || patch.cdp_attach === false))
    return settingsFieldError('This browser is not installed. Choose an installed browser.',form.elements.browser_channel);
  settingsSaving = true; settingsError = ''; $('#settings-error').textContent = ''; updateSettingsStatus();
  try {
    const expected = Object.fromEntries(Object.keys(patch).filter(k => k !== 'webhook').map(k => [k, settingsBaseline[k]]));
    const saved = await api('settings','POST',{...patch,_expected:expected});
    state.settings = [saved]; settingsDraft = null; settingsBaseline = null;
    settingsSavedMessage = Object.keys(patch).some(key => key.startsWith('fingerprint_') || ['browser_channel','show_browser_window','cdp_attach','cdp_endpoint','browser_timeout_ms','interaction_pacing'].includes(key))
      ? 'Saved. Browser changes apply to new sessions.' : 'Saved. All changes are up to date.';
    settingsSaving = false;
    $('#screen').innerHTML = settingsView(); updateSettingsStatus();
    await refresh();
  } catch (err) {
    settingsSaving = false;
    // Restore disabled controls by rebuilding from the retained draft.
    $('#screen').innerHTML = settingsView(); updateSettingsStatus();
    const key = Object.keys(patch).find(k => err.message.includes(k));
    settingsFieldError(err.message, key ? $('#settings-form').elements[key] : null);
  }
}

function troubleshootingView() {
  return `<div class="troubleshooting"><button type="button" data-view="settings">← Settings (drafts kept)</button>${settingCard('Activity', '', '<div class="setting-actions"><button type="button" data-context-view="events">Activity log</button><button type="button" data-context-view="feed">Monitor observations</button></div>')}${settingCard('AI browser recovery tests','These tests make API calls and may incur provider charges. They use a local fixture, without opening retailer accounts or placing orders.',(state.ai_connections || []).map(c => `<article class="connection-card"><h4>${esc(c.name)}</h4><p>${esc(c.browser_health?.message || 'Not tested')}</p><button type="button" data-ai-recovery="${esc(c.id)}">Test browser recovery</button></article>`).join('') || '<p>Add a connection in Settings first.</p>')}${settingCard('Recent AI activity','',(state.agent_runs || []).slice(-8).reverse().map(r => `<p>${esc(r.action)} · ${esc(r.status)} · ${r.steps || 0} model calls</p>`).join('') || '<p>No activity yet.</p>')}${settingCard('Diagnostic evidence','Traces can contain account details.', '<div class="diagnostic-list">' + ((state.diagnostics || []).map(d => `<button type="button" data-diagnostic="${esc(d.id)}">${esc(d.action)} · ${esc(time(d.at))}</button>`).join('') || '<p>No diagnostic evidence recorded.</p>') + '</div>' + repairsView())}</div>`;
}

document.addEventListener('input', event => {
  if (event.target.closest('#settings-form')) { captureSettingsDraft(); settingsSavedMessage = ''; updateSettingsStatus(); }
});
document.addEventListener('change', event => {
  if (!event.target.closest('#settings-form')) return;
  if (event.target.name === 'webhook_action' && event.target.value === 'remove') $('#settings-form').elements.notifications.checked = false;
  captureSettingsDraft(); settingsSavedMessage = ''; updateSettingsStatus();
});
document.addEventListener('submit', event => {
  if (event.target.id === 'settings-form') { event.preventDefault(); saveSettings(event.target); }
});
document.addEventListener('click', async event => {
  const b = event.target.closest('button'); if (!b) return;
  if (b.hasAttribute('data-refresh-troubleshooting')) { await refresh(); refreshSettingsView(); }
  if (b.hasAttribute('data-settings-discard')) {
    if (!confirm('Discard all unsaved settings changes?')) return;
    await refresh(); settingsDraft = null; settingsError = ''; settingsSavedMessage = '';
    $('#screen').innerHTML = settingsView(); updateSettingsStatus();
  }
  if (b.hasAttribute('data-test-discord')) {
    b.disabled = true;
    try { const result = await api('settings/test-discord','POST'); $('#settings-form [data-discord-result]').textContent = result.message; }
    catch (err) { const result = $('#settings-form [data-discord-result]'); if (result) result.textContent = err.message; }
    finally { if (b.isConnected) b.disabled = false; }
  }
});
document.addEventListener('click', event => {
  if (settingsSaving && event.target.closest('button,a')) {
    event.preventDefault(); event.stopImmediatePropagation();
  }
}, true);
document.addEventListener('keydown', event => {
  const tab = event.target.closest('[data-settings-tab]'); if (!tab) return;
  const keys = settingsSections.map(x => x[0]), index = keys.indexOf(tab.dataset.settingsTab);
  let next;
  if (['ArrowDown','ArrowRight'].includes(event.key)) next = (index + 1) % keys.length;
  if (['ArrowUp','ArrowLeft'].includes(event.key)) next = (index - 1 + keys.length) % keys.length;
  if (event.key === 'Home') next = 0;
  if (event.key === 'End') next = keys.length - 1;
  if (next !== undefined) { event.preventDefault(); activateSettingsTab(keys[next],true); }
});
window.addEventListener('beforeunload', event => {
  if (settingsDirty() || settingsSaving) { event.preventDefault(); event.returnValue = ''; }
});
window.addEventListener('resize', () => { if (view === 'settings') updateSettingsStatus(); });
