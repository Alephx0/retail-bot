/* AI connections are edited independently so key fields never enter general settings. */
const originalAISettingsView = settingsView;
settingsView = function () {
  const markup = originalAISettingsView();
  const template = document.createElement('template');
  template.innerHTML = markup;
  const s = state.settings[0] || {};
  const workerLabel = template.content.querySelector('label[for="f-max_running_tasks"]');
  if(workerLabel)workerLabel.textContent='Maximum active browser workers (extra tasks queue)';
  const browser = template.content.querySelector('[data-pane="browser"]');
  browser.innerHTML = '<p class="help">Tasks run in truly headless Chromium by default. View live shows the page; Take Control sends your clicks and typing to that same account page when a task is paused. Native OS passkey dialogs may require a separate visible session.</p>' +
    check('show_browser_window', 'Use a visible Chrome window from the start (special cases)', s.show_browser_window ?? false) +
    '<p class="help">Changing browser mode requires no running tasks. External CDP browsers follow their own window setting. Headless mode uses less desktop rendering resources and does not open a Chrome window.</p>' +
    '<details><summary>Advanced: use an existing Chrome or Edge window</summary>' + browser.innerHTML +
    check('cdp_attach', 'Use existing Chromium debugging connection', s.cdp_attach ?? false) +
    '<p class="help">Only use this if you already start a dedicated browser with remote debugging enabled. The app creates separate task tabs and does not use your personal tabs.</p></details>';
  const integrations = template.content.querySelector('[data-pane="integrations"]');
  const legacyIntegrations = integrations.innerHTML;
  integrations.innerHTML = '<h2>AI browser assistance</h2><p class="help">Add an OpenAI API key, choose a model, and save. Browser tools start automatically when a task needs them.</p>' +
    '<button type="button" data-ai-add>Add OpenAI API key</button><div class="ai-connections">' +
    (state.ai_connections || []).map(c => `<div class="panel"><strong>${esc(c.name)}</strong><p>${esc(c.provider)} · ${esc(c.model)} · ${c.has_api_key ? 'Key saved' : 'No key saved'}</p><p>${esc(c.health?.message || 'Not tested')}</p><p>${esc(c.browser_health?.message || 'Browser recovery not tested yet')}</p>${c.browser_health?.scenarios?.length?`<ul>${c.browser_health.scenarios.map(s=>`<li>${s.ok?'Passed':'Failed'}: ${esc(s.name)}</li>`).join('')}</ul>`:''}<button type="button" data-ai-edit="${c.id}">Edit</button> <button type="button" data-ai-test="${c.id}">Test connection</button> <button type="button" data-ai-recovery="${c.id}">Test browser recovery</button> <button type="button" data-ai-delete="${c.id}">Delete</button></div>`).join('') + '</div>' +
    '<p class="help">Test browser recovery checks changed cart and checkout buttons, a popup, duplicate links, a renamed final total, and simulated order confirmation using your model, MCP tools and CDP. It makes API calls (provider charges may apply), but uses only a local fixture and never opens your Amazon account or places a real order.</p>' +
    select('ai_connection_id', 'Active AI connection', [['', 'Choose a connection'], ...(state.ai_connections || []).map(c => [c.id, c.name + ' · ' + c.model])], s.ai_connection_id || '') +
    select('agent_mode', 'When should AI help?', [['recovery', 'Automatically recover checkout navigation (recommended)'], ['agent', 'At each cart and checkout step'], ['off', 'Off']], s.agent_mode || 'off') +
    '<p class="help">AI sees button and link labels and the page address without private URL parameters. Your API provider may charge for requests. Checkout still checks the item, seller and final price.</p>' +
    '<p class="help">Setup: save and test your API key, select it above, choose automatic recovery, then Save settings. Start a Live task with Automatic checkout. The app manages the browser, CDP and MCP for you: no debugging port or diagnosis endpoint is needed. AI automatically proposes and validates changed controls, including checkout continuation links and optional offer dismissal. It never rewrites production code or bypasses unreadable item, quantity or price checks. Monitor only never carts; Cart + browser review leaves the final order to you.</p>' +
    '<details><summary>Recent AI activity</summary>' + ((state.agent_runs || []).slice(-8).reverse().map(r => `<p>${esc(r.action)} · ${esc(r.status)} · ${r.steps || 0} model calls${r.message ? '<br>' + esc(r.message) : ''}</p>`).join('') || '<p>No browser recovery calls yet. A successful connection test is separate from a task run.</p>') + '</details>' +
    '<details><summary>Advanced AI settings</summary>' +
    input('agent_max_steps', 'Maximum model calls per action', s.agent_max_steps ?? 4, 'number', 'min="1" max="8"') +
    input('agent_timeout_seconds', 'Agent timeout (seconds)', s.agent_timeout_seconds ?? 60, 'number', 'min="10" max="180"') +
    '<p class="help">A local diagnosis endpoint is optional and only needed for legacy repair suggestions without a saved API connection.</p>' + legacyIntegrations + '</details>';
  return template.innerHTML;
};

const originalAISerialize = serializeSettings;
serializeSettings = function (data, form) {
  originalAISerialize(data, form);
  data.cdp_attach = form.elements.cdp_attach.checked;
  data.show_browser_window = form.elements.show_browser_window.checked;
  for (const key of ['agent_max_steps', 'agent_timeout_seconds']) data[key] = Number(data[key]);
};

function editAIConnection(id) {
  const value = (state.ai_connections || []).find(c => c.id === id) || {};
  const models = [['gpt-6-sol', 'GPT-6 Sol · Recommended balance'], ['gpt-6-luna', 'GPT-6 Luna · Lowest cost'], ['gpt-6-astra', 'GPT-6 Astra · Most capable'],
    ['gpt-5.6-sol', 'GPT-5.6 Sol'], ['gpt-5.6-terra', 'GPT-5.6 Terra'], ['gpt-5.6-luna', 'GPT-5.6 Luna'],
    ['gpt-5.4-mini', 'GPT-5.4 Mini'], ['gpt-4.1-mini', 'GPT-4.1 Mini']];
  if (value.model && !models.some(([id]) => id === value.model)) models.push([value.model, value.model + ' · Saved model']);
  const d = dialog('<form id="ai-connection-editor"><h2>' + (id ? 'Edit' : 'Add') + ' API connection</h2>' +
    '<p class="help">Create an API key in your <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener noreferrer">OpenAI account</a>, paste it below, and choose a model. GPT-6 Sol is the starting choice.</p>' +
    input('name', 'Connection name', value.name || 'My OpenAI key', 'text', 'required maxlength="100"') +
    secret('api_key', 'API key', value.has_api_key) +
    '<div data-openai-model>' + select('model', 'OpenAI model', models, value.model || 'gpt-6-sol') + '</div>' +
    '<div data-custom-model hidden>' + input('model_other', 'Provider model ID', value.provider === 'compatible' ? value.model || '' : '', 'text') + '</div>' +
    '<details><summary>Advanced: another API provider</summary>' +
    select('provider', 'API provider', [['openai', 'OpenAI'], ['compatible', 'OpenAI-compatible provider']], value.provider || 'openai') +
    select('protocol', 'API protocol', [['responses', 'Responses'], ['chat', 'Chat Completions']], value.protocol || 'responses') +
    input('base_url', 'API base URL', value.base_url || 'https://api.openai.com/v1', 'url', 'required') +
    check('clear_api_key', 'Remove saved API key', false) +
    '<p class="help">Other services must support the selected protocol and function tools.</p></details>' +
    '<p class="help">Keys are encrypted locally and hidden after saving. A blank key preserves the existing key.</p><p data-ai-error role="alert"></p><div class="modal-actions"><button type="button" data-cancel>Cancel</button><button class="primary" type="submit">Save connection</button></div></form>');
  const form = d.querySelector('form');
  function providerFields() {
    const openai = form.elements.provider.value === 'openai';
    if (openai) { form.elements.protocol.value = 'responses'; form.elements.base_url.value = 'https://api.openai.com/v1'; }
    form.elements.protocol.disabled = openai;
    form.elements.base_url.readOnly = openai;
    form.querySelector('[data-openai-model]').hidden = !openai;
    form.querySelector('[data-custom-model]').hidden = openai;
  }
  providerFields();
  form.elements.provider.onchange = providerFields;
  d.querySelector('[data-cancel]').onclick = () => d.close();
  form.onsubmit = async e => {
    e.preventDefault();
    const submit = form.querySelector('[type=submit]');
    submit.disabled = true;
    try {
      const data = Object.fromEntries(new FormData(form));
      data.protocol = form.elements.protocol.value;
      data.model = form.elements.provider.value === 'compatible' ? form.elements.model_other.value : form.elements.model.value;
      delete data.model_other;
      data.clear_api_key = form.elements.clear_api_key.checked;
      if (!id && !data.api_key) throw new Error('Paste an API key to create the connection');
      const saved = await api('ai_connections' + (id ? '/' + id : ''), id ? 'PUT' : 'POST', data);
      if (!id && !(state.settings[0] || {}).ai_connection_id) {
        await api('settings', 'POST', {ai_connection_id: saved.id, agent_mode: 'recovery'});
      }
      d.close(); await refresh(); $('#screen').innerHTML = settingsView();
      toast(id ? 'API connection saved' : 'AI assistance enabled. Test your connection before starting a task.');
    } catch (err) { d.querySelector('[data-ai-error]').textContent = err.message; }
    finally { submit.disabled = false; }
  };
}

document.addEventListener('click', async event => {
  const b = event.target.closest('button');
  if (!b || !['aiAdd', 'aiEdit', 'aiTest', 'aiRecovery', 'aiDelete'].some(k => k in b.dataset)) return;
  b.disabled = true;
  try {
    if ('aiAdd' in b.dataset) editAIConnection();
    if (b.dataset.aiEdit) editAIConnection(b.dataset.aiEdit);
    if (b.dataset.aiTest) {
      const result = await api('ai-connections/' + b.dataset.aiTest + '/test', 'POST');
      await refresh(); $('#screen').innerHTML = settingsView(); toast(result.message);
    }
    if (b.dataset.aiRecovery) {
      b.textContent = 'Testing AI + browser…';
      const result = await api('ai-connections/' + b.dataset.aiRecovery + '/test-recovery', 'POST');
      await refresh(); $('#screen').innerHTML = settingsView(); toast(result.message);
    }
    if (b.dataset.aiDelete) {
      await api('ai_connections/' + b.dataset.aiDelete, 'DELETE');
      await refresh(); $('#screen').innerHTML = settingsView();
    }
  } catch (err) { toast(err.message); }
  finally { b.disabled = false; }
});
