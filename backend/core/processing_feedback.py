"""Bounded, process-local feedback for single-image webhook events (WORKERS=1).

Only opaque event keys and reason codes are retained, never images or identities.
Methods run on the API event loop; no await occurs inside a cache operation.
"""
import time

TTL = 600
MAX_EVENTS = 5000
_events = {}

def lookup(key):
    now = time.monotonic()
    for old, record in list(_events.items()):
        if record[0] <= now:
            _events.pop(old, None)
    record = _events.get(key)
    return record[1] if record else None

def reserve(key):
    lookup(key)
    if len(_events) >= MAX_EVENTS:
        return False
    _events[key] = (time.monotonic() + TTL, 'pending')
    return True

def complete(key, status):
    if key in _events:
        _events[key] = (time.monotonic() + TTL, status)

def discard(key):
    _events.pop(key, None)
