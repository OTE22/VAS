"""Bounded, process-local feedback for single-image webhook events (WORKERS=1).

Only opaque event keys and reason codes are retained, never images or identities.
Methods run on the API event loop; no await occurs inside a cache operation.
"""
from collections import OrderedDict
import time
import hashlib
import re

TTL = 600
# Metadata budget: 150 events/s for ten minutes plus burst/backlog headroom.
# This is not an inference throughput claim; no images are retained here.
MAX_EVENTS = 100_000
_events = OrderedDict()


def _expire(now):
    # Fixed TTL and monotonic timestamps keep records in expiry order.
    # Completion moves refreshed records to the end. No full-cache scan on GET.
    while _events:
        token = next(iter(_events))
        if _events[token][0] > now:
            break
        _events.popitem(last=False)


def token_for(key):
    """Opaque, deterministic handle; never contains a pipeline ID or payload."""
    return hashlib.sha256(str(key).encode('utf-8')).hexdigest()


def lookup(key):
    return lookup_token(token_for(key))


def lookup_token(token):
    if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token):
        return None
    _expire(time.monotonic())
    record = _events.get(token)
    return record[1] if record else None

def reserve(key):
    now = time.monotonic()
    _expire(now)
    token = token_for(key)
    if token in _events:
        return True
    if len(_events) >= MAX_EVENTS:
        return False
    _events[token] = (now + TTL, 'pending')
    return True

def complete(key, status):
    now = time.monotonic()
    _expire(now)
    token = token_for(key)
    if token in _events:
        _events[token] = (now + TTL, status)
        _events.move_to_end(token)

def discard(key):
    _events.pop(token_for(key), None)
