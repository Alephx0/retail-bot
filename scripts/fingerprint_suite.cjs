// JSON stdin/stdout bridge. No account credentials or browser handles enter Node.
// Seed only this short-lived generator process; browser Math.random is untouched.
const fs = require('node:fs');
const path = require('node:path');
const version = name => JSON.parse(fs.readFileSync(path.join(path.dirname(require.resolve(name)), 'package.json'), 'utf8')).version;

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  if (!Number.isInteger(input.seed) || !Number.isInteger(input.browserMajor)) {
    throw new Error('An integer account seed and browser major version are required');
  }
  let state = input.seed >>> 0;
  Math.random = () => {
    state = (state + 0x6D2B79F5) >>> 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t ^= t + Math.imul(t ^ (t >>> 7), 61 | t);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const { FingerprintGenerator } = require('fingerprint-generator');
  const { newInjectedContext } = require('fingerprint-injector');
  const constraints = {
    browsers: ['chrome'],
    operatingSystems: [input.operatingSystem], devices: ['desktop'],
    locales: [input.locale], strict: true,
  };
  const generated = new FingerprintGenerator(constraints).getFingerprint();
  const scripts = [];
  let options, headers;
  // Use upstream context setup/header filtering/script generation verbatim.
  // Python applies the resulting operations through Patchright's equivalent API.
  const browser = {
    async newContext(value) {
      options = value;
      return {
        _options: { extraHTTPHeaders: Object.entries(value.extraHTTPHeaders || {}).map(([name, value]) => ({ name, value })) },
        browser: () => ({ browserType: () => ({ name: () => 'chromium' }) }),
        async setExtraHTTPHeaders(value) { headers = value; },
        async addInitScript({ content }) { scripts.push(content); },
        on(event, callback) {
          if (event !== 'page') throw new Error(`Unexpected upstream event: ${event}`);
          callback({ emulateMedia: async value => { options.colorScheme = value.colorScheme; } });
        },
      };
    },
  };
  await newInjectedContext(browser, { fingerprint: generated });
  const { screen } = generated.fingerprint;
  process.stdout.write(JSON.stringify({
    schema: 1, versions: {
      generator: version('fingerprint-generator'),
      injector: version('fingerprint-injector'),
    }, constraints, runtimeBrowserMajor: input.browserMajor,
    generatedBrowserMajor: Number(generated.fingerprint.navigator.userAgent.match(/Chrome\/(\d+)/)?.[1]),
    fingerprint: generated.fingerprint,
    options: {
      user_agent: options.userAgent, viewport: options.viewport,
      color_scheme: options.colorScheme, extra_http_headers: headers,
      locale: input.locale,
      screen: { width: screen.width, height: screen.height },
      device_scale_factor: screen.devicePixelRatio,
    }, scripts,
  }));
}
main().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
