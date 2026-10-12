"""Synthetic mailbox reproducing date-only search vs exact arrival timestamps."""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

from retail import identity


class Mailbox:
    def __init__(self, messages):
        self.messages = messages
        self.fetches = []
        self.searches = 0
        self.closed = False

    def response(self, name):
        assert name == 'UIDVALIDITY'
        return name, [b'123']

    def uid(self, action, *args):
        if action == 'search':
            self.searches += 1
            assert args[:2] == (None, 'SINCE')
            cutoff = datetime.strptime(args[2], '%d-%b-%Y').date()
            return 'OK', [b' '.join(str(i).encode() for i, m in enumerate(self.messages, 1) if m['search_date'] >= cutoff)]
        assert action == 'fetch' and args[1] == '(INTERNALDATE BODY.PEEK[])'
        self.fetches.append(args[0])
        m = self.messages[int(args[0])-1]
        stamp = m['received'].strftime('%d-%b-%Y %H:%M:%S %z')
        return 'OK', [(f'1 (INTERNALDATE "{stamp}" BODY[] {{100}}'.encode(), m['raw']), b')']

    def logout(self):
        self.closed = True


def message(received, *, sender='account-update@amazon.com', recipient='buyer@example.test', offset=-4):
    mail = EmailMessage()
    mail['From'], mail['To'] = sender, recipient
    mail.set_content('Your verification code is 123456.')
    return {'received': received, 'raw': mail.as_bytes(),
            'search_date': received.astimezone(timezone(timedelta(hours=offset))).date()}


@pytest.mark.parametrize('start', ['2026-10-12T00:24:08+00:00', '2026-11-01T00:01:00+00:00',
                                   '2027-01-01T00:01:00+00:00', '2026-10-11T20:24:08+00:00'])
def test_fresh_code_survives_mailbox_calendar_boundary(monkeypatch, start):
    since = datetime.fromisoformat(start)
    client = Mailbox([message(since + timedelta(seconds=11))])
    monkeypatch.setattr(identity, 'imap_connect', lambda _: client)
    result = identity.read_code({}, 'buyer@example.test', since.timestamp(), [])
    assert result == {'code': '123456', 'token': "[b'123']:1"}
    assert client.closed and client.searches == 1 and len(client.fetches) == 1


@pytest.mark.parametrize('mode', ['stale', 'used', 'wrong_recipient', 'wrong_sender', 'exact_boundary'])
def test_wider_search_preserves_freshness_scope_and_replay_checks(monkeypatch, mode):
    since = datetime(2026, 10, 12, 0, 24, 8, tzinfo=timezone.utc)
    received = since + timedelta(seconds=-1 if mode == 'stale' else 0)
    record = message(received, sender='attacker@example.test' if mode == 'wrong_sender' else 'account-update@amazon.com',
                     recipient='other@example.test' if mode == 'wrong_recipient' else 'buyer@example.test')
    client = Mailbox([record])
    monkeypatch.setattr(identity, 'imap_connect', lambda _: client)
    result = identity.read_code({}, 'buyer@example.test', since.timestamp(), ["[b'123']:1"] if mode == 'used' else [])
    assert bool(result) == (mode == 'exact_boundary')
    assert client.closed
    assert len(client.fetches) == (0 if mode == 'used' else 1)


def test_wider_search_retains_bounded_read_only_scan(monkeypatch):
    since = datetime(2026, 10, 12, 0, 24, 8, tzinfo=timezone.utc)
    client = Mailbox([message(since - timedelta(seconds=1)) for _ in range(100)])
    monkeypatch.setattr(identity, 'imap_connect', lambda _: client)
    assert identity.read_code({}, 'buyer@example.test', since.timestamp(), []) is None
    assert client.closed and client.searches == 1 and len(client.fetches) == 40
