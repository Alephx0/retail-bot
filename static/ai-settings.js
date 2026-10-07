/* Existing advanced profile controls; configuration behavior is unchanged. */
function legacyProfileSettings(s) {
  return '<details><summary>Fingerprint profiles</summary>' +
    select('fingerprint_backend', 'Profile implementation', [['javascript','JavaScript compatibility mode'],['native','Native Chromium profiles'],['fingerprint-suite','Apify fingerprint-suite (experimental)']], s.fingerprint_backend || 'javascript') +
    input('native_browser_executable', 'Native browser executable (blank uses installed build)', s.native_browser_executable || '') +
    '<p class="help">JavaScript graphics profiles keep GPU aliases within the hardware family and retain real WebGL capabilities. Workers initialize automatically. Each account uses a separate browser process, which uses more memory and requires an app-managed browser.</p>' +
    '<p class="help">Native profiles require the separately installed browser build. GPU identity is shared across WebGL, WebGPU and workers when either GPU option is enabled. Each active graphics profile uses its own browser process. This third-party build disables Safe Browsing.</p>' +
    '<p class="help">Fingerprint-suite applies its complete generated profile when selected. The custom switches below are inactive in this mode. It requires the optional Node packages and an app-managed browser; the upstream injector does not include our service-worker initialization.</p>' +
    check('fingerprint_canvas', 'Vary canvas rendering by account', s.fingerprint_canvas ?? false) +
    check('fingerprint_webgl', 'Vary WebGL identity and rendering', s.fingerprint_webgl ?? false) +
    check('fingerprint_webgpu', 'Vary WebGPU features and GPU identity', s.fingerprint_webgpu ?? false) +
    check('fingerprint_audio', 'Repair invalid audio metadata', s.fingerprint_audio ?? false) +
    check('fingerprint_workers', 'Initialize workers (automatic with graphics profiles)', s.fingerprint_workers ?? false) +
    check('fingerprint_fonts', 'Limit explicit local-font enumeration (preserve rendering)', s.fingerprint_fonts ?? false) +
    check('fingerprint_navigator', 'Vary CPU/memory and use the current Chrome browser identity', s.fingerprint_navigator ?? false) +
    check('fingerprint_screen', 'Vary desktop screen resolution and scaling by account', s.fingerprint_screen ?? false) +
    check('fingerprint_proxy_location', 'Match US profile location to the selected proxy', s.fingerprint_proxy_location ?? true) +
    select('fingerprint_timezone', 'US profile timezone', [['America/New_York','Eastern'],['America/Chicago','Central'],['America/Denver','Mountain'],['America/Los_Angeles','Pacific'],['America/Phoenix','Arizona'],['America/Anchorage','Alaska'],['Pacific/Honolulu','Hawaii']], s.fingerprint_timezone || 'America/New_York') +
    '<p class="help">The font, navigator and screen options apply to JavaScript profiles for US accounts. They use en-US. Proxy matching checks ipwho.is through the selected proxy before opening the account browser, then verifies the exit again. It sets the reported timezone and approximate location without granting site location permission. Use a sticky US proxy session: lookup failures, non-US exits and changes during setup stop the session. Without a proxy, or with matching off, the selected timezone applies.</p>' +
    '<p class="help">Values repeat for each account. Navigator profiles use the installed browser version and operating system, with Chrome identity in place of the HeadlessChrome marker across the account browser process; window mode is controlled separately. Font filtering respects site permission and does not hide fonts inferred from rendering. Screen and font choices are independent of the proxy city.</p>' +
    '</details>';
}

function syncFingerprintBackendFields(root) {
  const mode = root.querySelector('[name="fingerprint_backend"]')?.value;
  for (const name of ['canvas','webgl','webgpu','audio','workers','fonts','navigator','screen']) {
    const input = root.querySelector(`[name="fingerprint_${name}"]`);
    if (input) input.disabled = mode === 'fingerprint-suite' || (mode === 'native' && ['fonts','navigator','screen'].includes(name));
  }
  const timezone = root.querySelector('[name="fingerprint_timezone"]');
  if (timezone) timezone.disabled = mode !== 'javascript';
  const proxyLocation = root.querySelector('[name="fingerprint_proxy_location"]');
  if (proxyLocation) proxyLocation.disabled = mode !== 'javascript';
}
function editAIConnection(id) {
  const value = (state.ai_connections || []).find(c => c.id === id) || {};
  const models = [['gpt-6-sol', 'GPT-6 Sol · Recommended balance'], ['gpt-6-luna', 'GPT-6 Luna · Lowest cost'], ['gpt-6-astra', 'GPT-6 Astra · Most capable'],
    ['gpt-5.6-sol', 'GPT-5.6 Sol'], ['gpt-5.6-terra', 'GPT-5.6 Terra'], ['gpt-5.6-luna', 'GPT-5.6 Luna'],
    ['gpt-5.4-mini', 'GPT-5.4 Mini'], ['gpt-4.1-mini', 'GPT-4.1 Mini']];
  if (value.model && !models.some(([id]) => id === value.model)) models.push([value.model, value.model + ' · Saved model']);
  const d = dialog('<form id="ai-connection-editor"><h2>' + (id ? 'Edit' : 'Add') + ' API connection</h2>' +
    '<p class="help">Create an API key in your <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener noreferrer">OpenAI account</a>, paste it below, and choose a model. GPT-6 Sol is the starting choice.</p>' +
    input('name', 'Connection name', value.name || 'My OpenAI key', 'text', 'required maxlength="100"') +
    select('api_key_action', 'Saved credential', value.has_api_key ? [['keep','Keep saved key'],['replace','Replace key'],['remove','Remove key']] : [['replace','Add API key']], value.has_api_key ? 'keep' : 'replace') +
    '<div data-key-entry>' + secret('api_key', 'API key', value.has_api_key) + '</div>' +
    '<div data-openai-model>' + select('model', 'OpenAI model', models, value.model || 'gpt-6-sol') + '</div>' +
    '<div data-custom-model hidden>' + input('model_other', 'Provider model ID', value.provider === 'compatible' ? value.model || '' : '', 'text') + '</div>' +
    '<details><summary>Advanced: another API provider</summary>' +
    select('provider', 'API provider', [['openai', 'OpenAI'], ['compatible', 'OpenAI-compatible provider']], value.provider || 'openai') +
    select('protocol', 'API protocol', [['responses', 'Responses'], ['chat', 'Chat Completions']], value.protocol || 'responses') +
    input('base_url', 'API base URL', value.base_url || 'https://api.openai.com/v1', 'url', 'required') +
    '<p class="help">Other services must support the selected protocol and function tools.</p></details>' +
    '<p class="help">Keys are encrypted locally and hidden after saving. Saving a connection does not enable AI assistance.</p><p data-ai-error role="alert"></p><div class="modal-actions"><button type="button" data-cancel>Cancel</button><button class="primary" type="submit">Save connection</button></div></form>');
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
  function keyFields() {
    const replace = form.elements.api_key_action.value === 'replace';
    form.querySelector('[data-key-entry]').hidden = !replace;
    form.elements.api_key.disabled = !replace;
    form.elements.api_key.required = replace;
  }
  keyFields();
  form.elements.api_key_action.onchange = keyFields;
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
      data.clear_api_key = data.api_key_action === 'remove';
      delete data.api_key_action;
      if (!id && !data.api_key) throw new Error('Paste an API key to create the connection');
      await api('ai_connections' + (id ? '/' + id : ''), id ? 'PUT' : 'POST', data);
      d.close(); await refresh(); refreshSettingsView();
      toast(id ? 'API connection saved' : 'Connection saved. Test it, then explicitly enable assistance in Connections.');
    } catch (err) { d.querySelector('[data-ai-error]').textContent = err.message; }
    finally { submit.disabled = false; }
  };
}

document.addEventListener('click', async event => {
  const b = event.target.closest('button');
  if (!b || !['aiAdd', 'aiEdit', 'aiTest', 'aiRecovery', 'aiDelete'].some(k => k in b.dataset)) return;
  const originalText = b.textContent;
  b.disabled = true;
  try {
    if ('aiAdd' in b.dataset) editAIConnection();
    if (b.dataset.aiEdit) editAIConnection(b.dataset.aiEdit);
    if (b.dataset.aiTest) {
      const result = await api('ai-connections/' + b.dataset.aiTest + '/test', 'POST');
      await refresh(); refreshSettingsView(); toast(result.message);
    }
    if (b.dataset.aiRecovery) {
      b.textContent = 'Testing AI + browser…';
      const result = await api('ai-connections/' + b.dataset.aiRecovery + '/test-recovery', 'POST');
      await refresh(); refreshSettingsView(); toast(result.message);
    }
    if (b.dataset.aiDelete) {
      if (!confirm('Delete this API connection? This cannot be undone.')) return;
      await api('ai_connections/' + b.dataset.aiDelete, 'DELETE');
      await refresh(); refreshSettingsView();
    }
  } catch (err) { toast(err.message); }
  finally { b.disabled = false; b.textContent = originalText; }
});
