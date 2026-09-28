"""Bounded, process-local feedback for single-image webhook events (WORKERS=1).

Only opaque event keys and reason codes are retained, never images or identities.
Methods run on the API event loop; no await occurs inside a cache operation.
"""
import time
import hashlib
import re

TTL = 600
MAX_EVENTS = 5000
_events = {}

def token_for(key):
    """Opaque, deterministic handle; never contains a pipeline ID or payload."""
    return hashlib.sha256(str(key).encode('utf-8')).hexdigest()


def lookup(key):
    return lookup_token(token_for(key))


def lookup_token(token):
    if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token):
        return None
    now = time.monotonic()
    for old, record in list(_events.items()):
        if record[0] <= now:
            _events.pop(old, None)
    record = _events.get(token)
    return record[1] if record else None

def reserve(key):
    lookup(key)
    if len(_events) >= MAX_EVENTS:
        return False
    _events[token_for(key)] = (time.monotonic() + TTL, 'pending')
    return True

def complete(key, status):
    token = token_for(key)
    if token in _events:
        _events[token] = (time.monotonic() + TTL, status)

def discard(key):
    _events.pop(token_for(key), None)
