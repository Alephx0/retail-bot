# Gmail IMAP setup for Retail Desk

These steps are for a personal Gmail mailbox. Retail Desk currently supports IMAP username/app-password authentication, not Google OAuth. Personal Gmail already has IMAP enabled; there is no Enable IMAP toggle to switch on. [Google's current IMAP guidance](https://support.google.com/mail/answer/7126229?hl=en).

1. In your Google Account, open Security & sign-in and enable 2-Step Verification.
2. Open [Google App passwords](https://myaccount.google.com/apppasswords). Create an app password named **Retail Desk**. Copy the generated password into the local application, without the display spaces; use this instead of your usual Gmail password. [Google's app-password instructions](https://support.google.com/accounts/answer/185833?hl=en).
3. In Retail Desk, open **Tools → Mailboxes & codes → Add mailbox** and enter:

   | Field | Value |
   | --- | --- |
   | Mailbox name | A recognizable name, such as Signup Gmail |
   | IMAP host | `imap.gmail.com` |
   | TLS port | `993` |
   | Mailbox username | Your full Gmail address |
   | App password | The Google-generated app password |
   | Folder | `INBOX` |
   | Maximum code age (seconds) | `300` |

4. Save the mailbox, then click **Test connection**. Success says **TLS login and read-only folder access succeeded**. This tests login and folder access; it does not request an Amazon email or test code matching.
5. Before starting signup, open **Accounts → Account creation → New account → Region, connection & verification**. Select the saved mailbox under **Verification mailbox (IMAP)**. For an existing local account, use **Edit details → Connection & verification → Verification mailbox** before starting its browser.
6. Start signup. At email verification, Retail Desk looks for a fresh Amazon code addressed to the account's exact email address in the selected folder. Automatic lookup waits up to 180 seconds, then shows `Automatic email verification paused` if it cannot retrieve a matching code. It does not change the account to manual OTP mode. Keep Amazon verification messages in the selected folder. A running signup retains the account settings captured at startup; configure the mailbox before starting. If a registration is already submitted, finish its current verification in the open browser instead of resubmitting signup.

If connection testing fails, check the full username, generated app password, host and port. Changing your Google password revokes existing app passwords, so create a replacement and update the saved mailbox. App passwords may be unavailable with Advanced Protection, security-key-only 2-Step Verification, or organization policy; Retail Desk cannot connect an OAuth-only mailbox yet. [Google's restrictions and password-change behavior](https://support.google.com/accounts/answer/185833?hl=en).

If connection testing succeeds but no code arrives in the app, check the linked mailbox, recipient address, selected folder, Spam and message age. The reader does not reuse consumed messages, and signup only accepts messages received for the current attempt. Credentials stay in the existing encrypted vault; IMAP access is read-only.

Amazon account-update messages relayed by iCloud Hide My Email are supported when the specific rewritten sender, Hide My Email marker and relay/original DKIM-pass authentication-result headers are present. This uses mailbox delivery metadata, not independent cryptographic DKIM validation. The signup recipient must still match exactly; freshness and replay checks are unchanged. Arbitrary iCloud senders are rejected.

The date-only IMAP search starts one calendar day before the UTC freshness cutoff. This handles the observed evening date-boundary failure: searching October 12 returned no mail, while searching October 11 returned two matching messages received after the current signup began. The wider search does not relax freshness: each fetched message must still pass the exact `INTERNALDATE` timestamp cutoff, recipient/sender checks and consumed-message check. Each poll still searches once and fetches at most the newest 40 messages using `BODY.PEEK[]` in a read-only folder.

Regression command: `python -m pytest -q tests/test_imap_date_boundary.py tests/test_signup_email_code.py tests/test_infrastructure.py`. Validation on October 11, 2026: **59 passed in 33.36 seconds**, with the existing Starlette/httpx deprecation warning. The new fixture reproduces the failure before the fix at day, month and year boundaries, and checks stale messages, exact-cutoff messages, account scoping, replay prevention and bounded reads. Existing browser fixtures verify automatic email entry and submission, delayed arrival, cancellation and changed forms.

Controlled performance check: the synthetic `Mailbox` and `message` helpers in `tests/test_imap_date_boundary.py`, 100 messages received one second before a 20:24:08 UTC cutoff, 100 calls after five warmups. Before/after median was **0.405/0.402 ms**, p95 **0.470/0.455 ms**, with one search and 40 fetches per call. This excludes real IMAP authentication and network latency and does not establish a speedup. The fixed work bound is enforced by `test_wider_search_retains_bounded_read_only_scan`; widening the date filter may fetch messages that the earlier query excluded.

## Reactive signup email verification

Signup now opens one read-only mailbox connection for the active verification wait. It scans immediately, batches timestamps for up to the newest 40 candidates, and downloads bodies only for fresh, unused messages. It remembers inspected UIDs for that connection. After an empty scan it enters IMAP `IDLE` and rescans on new-message notifications, including updates received during a search/fetch or before the IDLE acknowledgement. It does not reconnect and sleep three seconds between attempts. Each account has its own bounded wait; shared-mailbox receipt claims remain atomic without locking out other accounts while waiting for mail.

The integration uses pinned `IMAPClient==3.0.1`. Its narrow `NotificationClient` adapter handles unsolicited responses before the IDLE acknowledgement; localhost protocol tests cover that compatibility behavior. Servers without `IDLE` keep their connection and use NOOP/search pacing derived from the measured scan duration. The existing 180-second verification deadline and 15-second stalled-I/O limit are safety bounds, not readiness delays. Closing or navigating the verification page cancels its wait. The original form is validated before consuming a code and again before submission. On Windows, an interrupted blocking read may take up to its I/O deadline to unwind; its worker owns cleanup and cannot claim a code or continue account actions after cancellation.

Exact recipient, sender/relay checks, timestamp freshness, the UTC date-boundary correction and durable consumed-message receipts remain authoritative. Manual one-shot code lookup keeps its existing behavior. Server-side recipient-header searching is deliberately avoided: a read-only trial on the configured provider timed out, whereas the date search and batched timestamps completed promptly.

Validation: `python -m pytest -q tests/test_imap_notifications.py tests/test_imap_date_boundary.py tests/test_signup_email_code.py tests/test_signup_verification.py tests/test_infrastructure.py tests/test_account_creation.py`. The broader run passed **99 tests** in **133.72 seconds** with the existing Starlette/httpx warning. After the final metadata-first reader and freshness recheck, `python -m pytest -q tests/test_imap_notifications.py tests/test_signup_email_code.py` passed **55 tests** in **31.14 seconds**. Desktop (1440px) and narrow (390px) email-waiting screenshots were visually inspected. Wire fixtures cover immediate and signalled arrival, scan/IDLE races, pre-acknowledgement notifications, keepalives, non-IDLE fallback, stale/used/wrong-account mail, shared-mailbox concurrency, atomic claims, UIDVALIDITY changes, deadlines, disconnects, cancellation and page validation failure. Browser fixtures cover close/navigation cancellation and single submission.

Reproduce the controlled latency comparison with `python scripts/benchmark_imap_notifications.py`: five runs per path, 39 unrelated synthetic messages and 20 ms simulated delay per command. Median arrival-to-code time was **4,443.8 ms before / 128.5 ms after**; maxima were **4,452.6 / 131.2 ms**. The old path made two logins and 40 individual fetches; the new path made one login and four batch fetches across the initial scan and arrival. Delivery is injected after the first search for the polling baseline and after listening starts for the new path; this isolates missed-poll and active-listener latency, not identical connection startup. No live provider delivery, browser typing or purchase time is included.

A read-only preflight against the configured mailbox confirmed IDLE support and a successful IDLE acknowledgement: connection/login/folder selection **1,009.6 ms**, timestamp scan **281.3 ms**, IDLE acknowledgement **110.2 ms** in that sample. No code was consumed. These variable network measurements do not guarantee instantaneous delivery; server delivery and connection setup still contribute latency.
