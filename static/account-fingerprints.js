'use strict';

const accountFingerprintOptions = [
  ['fingerprint_backend', 'Profile implementation', [['javascript','JavaScript compatibility mode'],['native','Native Chromium profiles'],['fingerprint-suite','Apify fingerprint-suite (experimental)']]],
  ...[['canvas','Canvas rendering'],['webgl','WebGL identity and rendering'],['webgpu','WebGPU features and GPU identity'],['audio','Audio metadata repair'],['workers','Worker initialization'],['fonts','Local font enumeration'],['navigator','CPU, memory and browser identity'],['screen','Screen resolution and scaling'],['proxy_location','Match location to account proxy']].map(([key,label]) => ['fingerprint_'+key,label,[['true','On'],['false','Off']]]),
  ['fingerprint_timezone','US profile timezone',['America/New_York','America/Chicago','America/Denver','America/Los_Angeles','America/Phoenix','America/Anchorage','Pacific/Honolulu'].map(value => [value,value])]
];
const fingerprintLaunches = new Set();

function accountFingerprintFields(account={}) {
  const global = state.settings?.[0] || {}, overrides = account.fingerprint_overrides || {};
  return '<details class="account-fingerprint-fields"><summary>Fingerprint profile</summary><p class="help">Use global follows Settings automatically. Choose an override for any option that should differ for this account. Changes apply to new browsers.</p>' +
    accountFingerprintOptions.map(([key,label,options]) => {
      const current = String(global[key] ?? (key === 'fingerprint_backend' ? 'javascript' : key === 'fingerprint_proxy_location'));
      const globalLabel = options.find(([value]) => value === current)?.[1] || current;
      return select('account_'+key,label,[['',`Use global (${globalLabel})`],...options],overrides[key] == null ? '' : String(overrides[key]));
    }).join('') +
    '<p class="help">Fingerprint-suite uses a complete profile; custom toggles are inactive. Native profiles use the executable configured in Settings; font, navigator, screen and US location controls apply only to JavaScript profiles. Graphics profiles initialize workers automatically.</p><button type="button" data-reset-fingerprint>Use global for all options</button></details>';
}

function collectAccountFingerprint(form) {
  const overrides = {};
  for (const [key] of accountFingerprintOptions) {
    const value = form.elements['account_'+key]?.value;
    if (value) overrides[key] = value === 'true' ? true : value === 'false' ? false : value;
  }
  return overrides;
}

function syncAccountFingerprintFields(root) {
  const backend = root.querySelector('[name=account_fingerprint_backend]')?.value || state.settings[0].fingerprint_backend;
  for (const [key] of accountFingerprintOptions) {
    const control = root.querySelector(`[name="account_${key}"]`);
    if (control) control.disabled = key !== 'fingerprint_backend' && (backend === 'fingerprint-suite' ||
      (backend === 'native' && ['fingerprint_fonts','fingerprint_navigator','fingerprint_screen','fingerprint_proxy_location','fingerprint_timezone'].includes(key)));
  }
}

function fingerprintSummary(account) {
  return Object.values(account.fingerprint_overrides || {}).some(value => value != null) ? 'Fingerprint overrides' : 'Global fingerprint';
}

async function launchAccountFingerprint(id) {
  if (fingerprintLaunches.has(id)) return;
  fingerprintLaunches.add(id);
  try {
    const result = await api(`accounts/${id}/test-fingerprint`, 'POST');
    toast(result.failed_sites?.length ? `Browser opened. Could not load: ${result.failed_sites.join(', ')}. Retry in those tabs.` : 'Fingerprint test browser opened.');
  } finally { fingerprintLaunches.delete(id); await refresh(); }
}

function editAccountFingerprint(id) {
  const account = state.accounts.find(item => item.id === id);
  const d = dialog(`<form><h2>Fingerprint · ${esc(account.name)}</h2>${accountFingerprintFields(account)}<p class="help">Testing opens a separate visible browser with this account’s fingerprint and proxy, without saved sign-in cookies. Starting websites are shared by all accounts. An empty list opens a blank tab.</p><p data-starting-sites-summary>${(state.settings[0].fingerprint_test_sites || []).map(site => esc(site.name)).join(' · ') || 'No starting websites'}</p><button type="button" data-fingerprint-sites>Manage starting websites</button><p data-fingerprint-error role="alert"></p><div class="modal-actions"><button type="button" data-dismiss-fingerprint>Cancel</button><button type="submit">Save</button><button type="submit" value="test" class="primary">Save & test</button></div></form>`);
  d.querySelector('details').open = true;
  syncAccountFingerprintFields(d);
  d.querySelector('[data-dismiss-fingerprint]').onclick = () => d.close();
  d.querySelector('form').onsubmit = async event => {
    event.preventDefault();
    const buttons = d.querySelectorAll('button[type=submit]');
    buttons.forEach(button => button.disabled = true);
    try {
      await api(`accounts/${id}`, 'PUT', {fingerprint_overrides: collectAccountFingerprint(event.target)});
      if (event.submitter?.value === 'test') await launchAccountFingerprint(id);
      else { await refresh(); toast('Account fingerprint saved'); }
      d.close();
    } catch (error) { d.querySelector('[data-fingerprint-error]').textContent = error.message; }
    finally { buttons.forEach(button => button.disabled = false); }
  };
}

function editFingerprintSites() {
  const original = state.settings[0].fingerprint_test_sites || [];
  const d = dialog('<form><h2>Fingerprint test starting websites</h2><p class="help">These websites open in separate tabs for every account test. Use full HTTP or HTTPS URLs. Save an empty list to start with a blank tab.</p><div data-site-rows></div><button type="button" data-add-site>Add website</button><p data-site-error role="alert"></p><div class="modal-actions"><button type="button" data-cancel-sites>Cancel</button><button type="submit" class="primary">Save websites</button></div></form>');
  const add = (site={name:'',url:''}) => {
    const rows = d.querySelector('[data-site-rows]');
    if (rows.children.length >= 20) { d.querySelector('[data-site-error]').textContent = 'Maximum 20 starting websites.'; return; }
    const row = document.createElement('div');
    row.className = 'fingerprint-site-row';
    row.innerHTML = `<label>Website name<input data-site-name required maxlength="100" value="${esc(site.name)}"></label><label>URL<input data-site-url type="url" required maxlength="2048" placeholder="https://example.com/" value="${esc(site.url)}"></label><button type="button" aria-label="Delete website">Delete</button>`;
    row.querySelector('button').onclick = () => row.remove();
    rows.append(row);
  };
  original.forEach(add);
  d.querySelector('[data-add-site]').onclick = () => add();
  d.querySelector('[data-cancel-sites]').onclick = () => d.close();
  d.querySelector('form').onsubmit = async event => {
    event.preventDefault();
    const button = d.querySelector('[type=submit]'); button.disabled = true;
    try {
      const sites = [...d.querySelectorAll('.fingerprint-site-row')].map(row => ({name:row.querySelector('[data-site-name]').value.trim(),url:row.querySelector('[data-site-url]').value.trim()}));
      await api('settings','POST',{fingerprint_test_sites:sites,_expected:{fingerprint_test_sites:original}});
      await refresh();
      document.querySelectorAll('[data-starting-sites-summary]').forEach(summary => summary.textContent = sites.map(site => site.name).join(' · ') || 'No starting websites');
      d.close(); toast('Starting websites saved');
    } catch (error) { d.querySelector('[data-site-error]').textContent = error.message; }
    finally { button.disabled = false; }
  };
}

document.addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.hasAttribute('data-reset-fingerprint')) {
    const fields = button.closest('.account-fingerprint-fields');
    fields.querySelectorAll('select').forEach(input => input.value = '');
    syncAccountFingerprintFields(fields);
  }
  else if (button.hasAttribute('data-fingerprint-sites')) editFingerprintSites();
  else if (button.dataset.fingerprintEdit) editAccountFingerprint(button.dataset.fingerprintEdit);
  else if (button.dataset.fingerprintTest || button.dataset.fingerprintClose) {
    button.disabled = true;
    try {
      if (button.dataset.fingerprintTest) await launchAccountFingerprint(button.dataset.fingerprintTest);
      else { await api(`accounts/${button.dataset.fingerprintClose}/close-fingerprint-test`,'POST'); await refresh(); toast('Fingerprint test closed'); }
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  }
});

document.addEventListener('change', event => {
  if (event.target.name === 'account_fingerprint_backend') syncAccountFingerprintFields(event.target.closest('.account-fingerprint-fields'));
});
