"""Bounded in-memory stage timings. Never records arguments, URLs or secrets."""
import asyncio
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time

_scope = ContextVar('performance_scope', default=None)


class Performance:
    def __init__(self, limit=2048):
        self.samples = deque(maxlen=limit)
        self.active = {}
        self.sequence = 0

    @contextmanager
    def scope(self, kind, identifier):
        token = _scope.set((self, kind, identifier))
        try:
            yield
        finally:
            _scope.reset(token)

    def snapshot(self, kind='', identifier=''):
        def matches(row):
            return (not kind or row['kind'] == kind) and (not identifier or row['id'] == identifier)
        rows = [row.copy() for row in self.samples if matches(row)]
        summary = {}
        for row in rows:
            entry = summary.setdefault(row['stage'], {'stage': row['stage'], 'count': 0, 'total_ms': 0,
                                                       'max_ms': 0, 'last_ms': 0, 'errors': 0})
            entry['count'] += 1
            entry['total_ms'] += row['duration_ms']
            entry['max_ms'] = max(entry['max_ms'], row['duration_ms'])
            entry['last_ms'] = row['duration_ms']
            entry['errors'] += row['outcome'] == 'error'
        for entry in summary.values():
            entry['mean_ms'] = round(entry.pop('total_ms') / entry['count'], 2)
        return {'summary': sorted(summary.values(), key=lambda row: row['mean_ms'], reverse=True),
                'recent': rows[-200:], 'active': [{**row, 'duration_ms': round((time.monotonic()-start)*1000, 2)}
                                                for start, row in self.active.values() if matches(row)],
                'capacity': self.samples.maxlen}


@contextmanager
def stage(name):
    scope = _scope.get()
    if scope is None:
        yield
        return
    recorder, kind, identifier = scope
    recorder.sequence += 1
    sequence = recorder.sequence
    start = time.monotonic()
    row = {'kind': kind, 'id': identifier, 'stage': name, 'at': time.time(), 'outcome': 'ok'}
    recorder.active[sequence] = (start, row)
    try:
        yield
    except BaseException as exc:
        row['outcome'] = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'error'
        raise
    finally:
        recorder.active.pop(sequence, None)
        recorder.samples.append({**row, 'duration_ms': round((time.monotonic()-start)*1000, 2)})


def timed(name):
    def decorate(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            with stage(name):
                return await fn(*args, **kwargs)
        return wrapped
    return decorate


def elapsed(name, since):
    scope = _scope.get()
    if scope is not None:
        recorder, kind, identifier = scope
        recorder.samples.append({'kind': kind, 'id': identifier, 'stage': name, 'at': time.time(),
                                 'outcome': 'ok', 'duration_ms': round(max(0, time.monotonic()-since)*1000, 2)})
