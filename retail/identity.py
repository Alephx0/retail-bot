"""Local TOTP generation and narrowly scoped, read-only IMAP OTP retrieval."""
import asyncio
import base64
import email
import hashlib
import hmac
import html
import imaplib
import re
import ssl
import struct
import time
from datetime import datetime, timedelta, timezone
from email.policy import default
from email.utils import getaddresses


def totp(secret: str, timestamp=None, digits=6):
    secret = secret.replace(" ", "").upper().rstrip("=")
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int(time.time() if timestamp is None else timestamp) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return str((struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff) % 10 ** digits).zfill(digits)


def allowed_otp_sender(message, sender_domains):
    senders = getaddresses(message.get_all("From", []))
    if len(senders) != 1 or "@" not in senders[0][1]:
        return False
    local, domain = senders[0][1].lower().rsplit("@", 1)
    if domain in sender_domains:
        return True
    # Hide My Email preserves the recipient but rewrites Amazon's sender.
    # Require its specific alias plus relay/original authentication metadata;
    # never accept arbitrary iCloud mail or a display name containing Amazon.
    if domain != "icloud.com" or not message.get("X-ICLOUD-HME"):
        return False
    original = next((d for d in sender_domains if re.fullmatch(
        r"account-update_at_" + re.escape(d.replace('.', '_')) + r"_[a-z0-9_-]+", local)), None)
    if not original:
        return False
    results = message.get_all("Authentication-Results", [])
    def passed(header, domain):
        return any(re.search(r"\bdkim\s*=\s*pass\b", clause, re.I) and
                   re.search(r"\bheader\.(?:d|i)\s*=\s*@?" + re.escape(domain) + r"(?=\s|$)", clause, re.I)
                   for clause in str(header).split(';'))
    return bool(results and passed(results[0], 'icloud.com') and any(passed(result, original) for result in results))


def extract_otp(raw: bytes, recipient: str, sender_domains: tuple[str, ...]):
    message = email.message_from_bytes(raw, policy=default)
    if not allowed_otp_sender(message, sender_domains):
        return None
    recipients = getaddresses(sum((message.get_all(h, []) for h in ("To", "Delivered-To", "X-Original-To")), []))
    if recipient.lower() not in {a.lower() for _, a in recipients}:
        return None
    parts = [part for part in message.walk() if part.get_content_type() in ("text/plain", "text/html") and part.get_content_disposition() != "attachment"]
    body = " ".join(str(part.get_content()) for part in parts)
    body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    # Only a code associated with an OTP/security/verification label, not any 6 digits.
    patterns = [r"(?:one[ -]time password|verification code|security code|OTP)(?:\s*\(OTP\))?(?:\s|:|is|-){0,20}(\d{6})(?!\d)",
                r"(?<!\d)(\d{6})(?:\s|\.|:|-){1,15}(?:is your|is the)\s+(?:Amazon\s+)?(?:OTP|verification code|security code)"]
    for pattern in patterns:
        found = re.search(pattern, body, re.I)
        if found:
            return found[1]
    return None


def imap_connect(mailbox):
    client = imaplib.IMAP4_SSL(mailbox["host"], mailbox["port"], ssl_context=ssl.create_default_context(), timeout=15)
    try:
        client.login(mailbox["username"], mailbox["password"])
        status, _ = client.select('"' + mailbox["folder"] + '"', readonly=True)
        if status != "OK":
            raise ValueError("Mailbox folder is unavailable")
        return client
    except BaseException:
        try:
            client.logout()
        except Exception:
            pass
        raise


def test_mailbox(mailbox):
    client = imap_connect(mailbox)
    client.logout()
    return {"ok": True, "message": "TLS login and read-only folder access succeeded"}


def read_code(mailbox, recipient, since, used, sender_domains=("amazon.com", "amazon.co.uk", "amazon.ca")):
    client = imap_connect(mailbox)
    try:
        # Date-only mailbox searches can lag the UTC calendar after midnight.
        # Widen discovery, then enforce the exact INTERNALDATE cutoff below.
        search_day = datetime.fromtimestamp(since, timezone.utc) - timedelta(days=1)
        status, rows = client.uid("search", None, "SINCE", search_day.strftime("%d-%b-%Y"))
        if status != "OK":
            return None
        validity = client.response("UIDVALIDITY")[1]
        prefix = str(validity)
        for uid in rows[0].split()[-40:][::-1]:
            token = prefix + ":" + uid.decode()
            if token in used:
                continue
            status, rows = client.uid("fetch", uid, "(INTERNALDATE BODY.PEEK[])")
            if status != "OK":
                continue
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                stamp = imaplib.Internaldate2tuple(row[0])
                if not stamp or time.mktime(stamp) < since:
                    continue
                code = extract_otp(row[1], recipient, sender_domains)
                if code:
                    return {"code": code, "token": token}
    finally:
        client.logout()
    return None


class IdentityService:
    def __init__(self, store):
        self.store = store
        self.locks = {}

    async def wait_code(self, account, since=None, timeout=180, before_claim=None):
        """Read immediately, then wait for mailbox signals without reconnecting."""
        from .imap_wait import MailboxSession
        mailbox = self.store.get('mailboxes', account.get('mailbox_id', ''))
        if not mailbox:
            raise ValueError('Link an IMAP mailbox to receive email codes')
        session = MailboxSession(mailbox)
        deadline = time.monotonic() + timeout
        receipt_id = 'otp-' + mailbox['id']
        lock = self.locks.setdefault(mailbox['id'], asyncio.Lock())
        try:
            async with asyncio.timeout(timeout):
                await session.call(session.open)
                while True:
                    earliest = max(time.time() - mailbox['max_age_seconds'], since or 0)
                    receipt = self.store.get('otp_receipts', receipt_id) or {'tokens': []}
                    rows = await session.call(session.scan, earliest, set(receipt['tokens']))
                    for token, row in rows:
                        stamp = row.get(b'INTERNALDATE')
                        # Recheck age after network I/O; old mail never becomes fresh.
                        cutoff = max(time.time() - mailbox['max_age_seconds'], since or 0)
                        if not stamp or stamp.timestamp() < cutoff:
                            continue
                        code = extract_otp(row.get(b'BODY[]', b''), account['email'],
                                           ('amazon.com', 'amazon.co.uk', 'amazon.ca'))
                        if not code:
                            continue
                        if before_claim is not None:
                            await before_claim()
                        # Waiters on a shared mailbox must not hold this lock
                        # while waiting for their own account's email.
                        async with lock:
                            if stamp.timestamp() < max(time.time() - mailbox['max_age_seconds'], since or 0):
                                continue
                            receipt = self.store.get('otp_receipts', receipt_id) or {'tokens': []}
                            if token in receipt['tokens']:
                                continue
                            self.store.put('otp_receipts', {'tokens': (receipt['tokens'] + [token])[-200:]}, receipt_id)
                        return {'code': code, 'source': 'imap', 'expires_in': mailbox['max_age_seconds']}
                    await session.call(session.wait_for_change, max(0, deadline - time.monotonic()))
        finally:
            await session.close()

    async def code(self, account, since=None):
        if account.get("totp_secret"):
            return {"code": totp(account["totp_secret"]), "source": "authenticator", "expires_in": 30 - int(time.time()) % 30}
        mailbox = self.store.get("mailboxes", account.get("mailbox_id", ""))
        if not mailbox:
            raise ValueError("Configure an authenticator secret or link an IMAP mailbox")
        lock = self.locks.setdefault(mailbox["id"], asyncio.Lock())
        async with lock:
            receipt_id = "otp-" + mailbox["id"]
            receipt = self.store.get("otp_receipts", receipt_id) or {"tokens": []}
            earliest = max(time.time() - mailbox["max_age_seconds"], since or 0)
            result = await asyncio.to_thread(read_code, mailbox, account["email"], earliest, receipt["tokens"])
            if not result:
                raise ValueError("No fresh matching verification email found")
            self.store.put("otp_receipts", {"tokens": (receipt["tokens"] + [result["token"]])[-200:]}, receipt_id)
            return {"code": result["code"], "source": "imap", "expires_in": mailbox["max_age_seconds"]}
