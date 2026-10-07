# Account fingerprint profiles and testing

In **Accounts**, choose **Fingerprint** beside an account, or expand **Fingerprint
profile** when creating or editing an account. Profile implementation, surface
toggles, proxy location matching and timezone each offer **Use global** plus an
explicit override. Global values come from Settings and continue to apply to
every option that inherits them. **Use global for all options** removes overrides
when saved. New browsers use the effective settings; already open browsers keep
their current profile.

The implementation choices and their existing limitations match Settings.
Fingerprint-suite applies its complete generated profile instead of custom
surface toggles. Native profiles use the executable configured globally in
Settings. Browser channel, window mode and the native executable remain global.
Native and JavaScript accounts can run in separate processes simultaneously.

**Test fingerprint** opens the saved account profile in a separate visible browser.
**Save & test** first saves the fingerprint editor and then launches that profile.
Tests use the same stable account seed and account proxy as other account
browsers, without loading or saving sign-in cookies. They do not change task
window mode or run retailer sign-in automation. External CDP attachment must be
disabled to launch this separate managed browser.

**Test websites** manages the shared starting website list. CreepJS and Google
are supplied initially. Add, edit or delete rows, then **Save websites**. Each
website opens in its own tab; an empty list opens a blank tab. URLs must use HTTP
or HTTPS without embedded credentials. Up to 20 starting websites are supported.
Unavailable websites are reported without closing the other tabs.

Use **Close test** or close the browser window when finished. Testing the same
account again replaces its previous test browser. Up to five account test
browsers can be open at once. Deleting an account or shutting down the application
also closes its test browser.

Overrides are stored in `Account.fingerprint_overrides`. Missing or null values
inherit global settings; explicit false values override global true values.
Starting websites are stored in `Settings.fingerprint_test_sites`. The account
actions are `POST /api/accounts/{id}/test-fingerprint` and
`POST /api/accounts/{id}/close-fingerprint-test`.
