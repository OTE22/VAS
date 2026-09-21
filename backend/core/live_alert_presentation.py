"""Current alert labels, without rewriting stored titles or event history."""


def alert_display_name(alert):
    if not getattr(alert, 'auto_name', False):
        return alert.name
    identity = alert.identity
    name = (identity.display_name or '').strip() if identity else ''
    return f"Track {name or 'Unknown Person'}"


def alert_identity_type(alert):
    kind = getattr(alert.identity, 'type', None) if alert.identity else None
    return getattr(kind, 'value', kind) if kind is not None else None
