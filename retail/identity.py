"""Local TOTP generation and narrowly scoped, read-only IMAP OTP retrieval."""
import asyncio
import base64
import email
import hashlib
import hmac
import imaplib
import re
import ssl
import struct
import time
from datetime import datetime, timezone
from email.policy import default
from email.utils import getaddresses


def totp(secret: str, timestamp=None, digits=6):
    secret = secret.replace(" ", "").upper().rstrip("=")
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int(time.time() if timestamp is None else timestamp) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return str((struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff) % 10 ** digits).zfill(digits)


def extract_otp(raw: bytes, recipient: str, sender_domains: tuple[str, ...]):
    message = email.message_from_bytes(raw, policy=default)
    sender = getaddresses(message.get_all("From", []))
    if len(sender) != 1 or sender[0][1].rsplit("@", 1)[-1].lower() not in sender_domains:
        return None
    recipients = getaddresses(sum((message.get_all(h, []) for h in ("To", "Delivered-To", "X-Original-To")), []))
    if recipient.lower() not in {a.lower() for _, a in recipients}:
        return None
    parts = [part for part in message.walk() if part.get_content_type() in ("text/plain", "text/html") and part.get_content_disposition() != "attachment"]
    body = " ".join(str(part.get_content()) for part in parts)
    body = re.sub(r"<[^>]+>", " ", body)
    # Only a code associated with an OTP/security/verification label, not any 6 digits.
    patterns = [r"(?:one.time password|verification code|security code|OTP)(?:\s|:|is|-){0,20}(\d{6})(?!\d)",
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
        status, rows = client.uid("search", None, "SINCE", datetime.fromtimestamp(since, timezone.utc).strftime("%d-%b-%Y"))
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
