"""Normalize explicit camera times; otherwise preserve server receipt time."""
from datetime import datetime, timezone


def observation_time(value, received_at):
    if value is None:
        return received_at, 'server_received'
    if not isinstance(value, str):
        raise ValueError('captured_at must be an ISO-8601 timestamp with a timezone')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('captured_at must include a timezone')
    return stamp.astimezone(timezone.utc).replace(tzinfo=None), 'camera_reported'
