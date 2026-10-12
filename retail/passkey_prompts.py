"""Decline optional public-key credential prompts in app account browsers."""
SCRIPT = r'''(() => {
  if (!globalThis.CredentialsContainer || !globalThis.PublicKeyCredential) return;
  const prototype = CredentialsContainer.prototype;
  for (const method of ['create', 'get']) {
    const original = prototype[method];
    if (typeof original !== 'function') continue;
    Object.defineProperty(prototype, method, {configurable:true, writable:true, value:function(options) {
      if (options && options.publicKey)
        return Promise.reject(new DOMException('Passkey prompts are disabled in this account browser.', 'NotAllowedError'));
      return Reflect.apply(original, this, arguments);
    }});
  }
  for (const method of ['isUserVerifyingPlatformAuthenticatorAvailable', 'isConditionalMediationAvailable']) {
    if (typeof PublicKeyCredential[method] === 'function')
      Object.defineProperty(PublicKeyCredential, method, {configurable:true, value:() => Promise.resolve(false)});
  }
})();'''


async def install(context):
    await context.add_init_script(SCRIPT)
