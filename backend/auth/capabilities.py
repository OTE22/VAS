"""Canonical roles and capabilities — the single authorization vocabulary.

Why this module exists
----------------------
The admin UI offered three roles (`user`, `analyzer`, `Observer`) and the API
validated against exactly those three, but **no authorization check anywhere
tested for them**. Every gate in the codebase compared against `admin`:

    45x  require_role(["admin"])
    23x  role == "admin"
    23x  role != "admin"

So the system was effectively binary — admin or not-admin — and moving a user
to `analyzer` changed nothing observable. That is the reported bug: the role
was written to the database correctly and then consulted by nothing. Verified
live: a user promoted to `analyzer` still received 403 on admin routes (correct)
and an EMPTY `privileges` list from `/api/auth/me/privileges` (the symptom).

What this module does NOT do
----------------------------
It does not introduce a permissions table. There is none, and inventing one
would mean a speculative join-table migration for a permission surface that is
really four things: `users.role`, `users.can_use_chatbot`, `users.is_active`,
and `user_pipeline_access` rows. Capabilities are *derived* from those.

Resolution policy (one rule, applied everywhere)
------------------------------------------------
    effective = role capabilities
              + per-user grants (can_use_chatbot -> CHATBOT_USE)
              - everything, if the account is inactive

`can_use_chatbot` deliberately stays an independent per-user override rather
than being folded into the role map: it is a real column with real enforcement,
and rolling it into roles would silently change who has chatbot access.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Iterable, Optional


class Role(str, Enum):
    """Canonical role values.

    Lowercase, because `require_role` does a case-sensitive membership test
    (`auth_service.py`) and the database previously stored a capitalised
    "Observer" alongside lowercase peers — a mismatch waiting to deny access to
    the wrong person.
    """

    ADMIN = "admin"
    ANALYZER = "analyzer"
    USER = "user"
    OBSERVER = "observer"


# Historical spellings -> canonical. Applied on read and on write so a row
# written before the canonicalisation migration still resolves correctly.
_ROLE_ALIASES = {
    "observer": Role.OBSERVER,
    "Observer": Role.OBSERVER,
    "OBSERVER": Role.OBSERVER,
    "analyzer": Role.ANALYZER,
    "Analyzer": Role.ANALYZER,
    "analyst": Role.ANALYZER,      # the word the brief and the UI label use
    "user": Role.USER,
    "normal_user": Role.USER,
    "admin": Role.ADMIN,
    "administrator": Role.ADMIN,
}


def is_known_role(raw: Optional[str]) -> bool:
    """Whether `raw` names a role this system recognises.

    Validation MUST use this rather than comparing `canonical_role(raw)`
    against the assignable set. canonical_role deliberately falls back to the
    least-privileged role, so an unrecognised value like "wizard" would
    canonicalise to "observer" and sail through an assignable-set check —
    silently demoting the user instead of reporting the typo. That bug was
    caught in verification; this function is the fix.
    """
    if raw is None:
        return False
    text = str(raw).strip()
    return text in _ROLE_ALIASES or text.lower() in _ROLE_ALIASES


def canonical_role(raw: Optional[str]) -> Role:
    """Normalise any stored or submitted role string.

    Unknown values resolve to the LEAST privileged role, never the most, so a
    corrupted or unexpected stored value can never become an accidental
    promotion. Use is_known_role() to *reject* unknown input on write.
    """
    if not raw:
        return Role.OBSERVER
    text = str(raw).strip()
    if text in _ROLE_ALIASES:
        return _ROLE_ALIASES[text]
    lowered = text.lower()
    if lowered in _ROLE_ALIASES:
        return _ROLE_ALIASES[lowered]
    return Role.OBSERVER


class Capability(str, Enum):
    """What a user may do. Named after the action, not the role."""

    # Viewing
    DASHBOARD_VIEW = "dashboard.view"
    IDENTITY_VIEW = "identity.view"
    UNKNOWN_FACES_VIEW = "unknown_faces.view"
    INTELLIGENCE_VIEW = "intelligence.view"
    WATCHLIST_VIEW = "watchlist.view"

    # Analysis
    SEARCH_RUN = "search.run"
    EXPORT_CREATE = "export.create"
    IDENTITY_MANAGE = "identity.manage"
    WATCHLIST_MANAGE = "watchlist.manage"

    # Chatbot / SQL agent
    CHATBOT_USE = "chatbot.use"
    CHATBOT_HISTORY_READ = "chatbot.history.read"
    CHATBOT_HISTORY_DELETE = "chatbot.history.delete"

    # Administration
    USERS_MANAGE = "admin.users.manage"
    SETTINGS_MANAGE = "admin.settings.manage"
    AUDIT_READ = "admin.audit.read"
    PIPELINE_MANAGE = "admin.pipeline.manage"
    ML_MODEL_MANAGE = "admin.ml_model.manage"
    ML_MANAGE = "admin.ml.manage"
    RETENTION_MANAGE = "admin.retention.manage"


# Cumulative, least privilege first. Each tier includes the one before it, so
# adding a capability to OBSERVER grants it to everyone by construction — which
# is the intended reading of "observer < user < analyzer < admin".
_OBSERVER: FrozenSet[Capability] = frozenset({
    Capability.DASHBOARD_VIEW,
    Capability.IDENTITY_VIEW,
})

_USER: FrozenSet[Capability] = _OBSERVER | frozenset({
    Capability.SEARCH_RUN,
    Capability.UNKNOWN_FACES_VIEW,
})

_ANALYZER: FrozenSet[Capability] = _USER | frozenset({
    Capability.IDENTITY_MANAGE,
    Capability.EXPORT_CREATE,
    Capability.INTELLIGENCE_VIEW,
    Capability.WATCHLIST_VIEW,
    Capability.CHATBOT_HISTORY_READ,
})

_ADMIN: FrozenSet[Capability] = frozenset(Capability)

ROLE_CAPABILITIES = {
    Role.OBSERVER: _OBSERVER,
    Role.USER: _USER,
    Role.ANALYZER: _ANALYZER,
    Role.ADMIN: _ADMIN,
}


class EffectiveAuthorization:
    """The resolved authorization for one user at one moment.

    Immutable snapshot. `permissions_version` is carried so a long-lived
    connection can detect that its snapshot is stale with one integer
    comparison instead of re-resolving on every message.
    """

    __slots__ = ("user_id", "username", "role", "is_active",
                 "capabilities", "permissions_version")

    def __init__(self, *, user_id: int, username: str, role: Role,
                 is_active: bool, capabilities: FrozenSet[Capability],
                 permissions_version: int):
        self.user_id = user_id
        self.username = username
        self.role = role
        self.is_active = is_active
        self.capabilities = capabilities
        self.permissions_version = permissions_version

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def has_all(self, capabilities: Iterable[Capability]) -> bool:
        return all(c in self.capabilities for c in capabilities)

    @property
    def permission_codes(self) -> list:
        """Stable string codes, sorted — what the API returns to clients.

        Stable identifiers, never UI labels, so the frontend can compare them
        and the values survive translation.
        """
        return sorted(c.value for c in self.capabilities)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"EffectiveAuthorization(user_id={self.user_id}, "
                f"role={self.role.value}, caps={len(self.capabilities)}, "
                f"v={self.permissions_version})")


def resolve_effective_authorization(user) -> EffectiveAuthorization:
    """The one resolver. Every gate in the application must go through this.

    Takes an already-loaded User row rather than a session + id, because
    `get_current_user` has already paid for that query on every request —
    re-querying here would double the cost of authorizing a request.
    """
    role = canonical_role(getattr(user, "role", None))
    is_active = bool(getattr(user, "is_active", False))

    if not is_active:
        # An inactive account holds no capabilities, whatever its role says.
        capabilities: FrozenSet[Capability] = frozenset()
    else:
        capabilities = ROLE_CAPABILITIES.get(role, _OBSERVER)
        if getattr(user, "can_use_chatbot", False):
            capabilities = capabilities | {Capability.CHATBOT_USE}
            # Using the chatbot implies being able to read your own history;
            # granting one without the other produces a chatbot that cannot
            # show what it just answered.
            capabilities = capabilities | {
                Capability.CHATBOT_HISTORY_READ,
                Capability.CHATBOT_HISTORY_DELETE,
            }

    return EffectiveAuthorization(
        user_id=getattr(user, "id", 0),
        username=getattr(user, "username", ""),
        role=role,
        is_active=is_active,
        capabilities=capabilities,
        permissions_version=int(getattr(user, "permissions_version", 1) or 1),
    )


# Roles an administrator may assign through the API. `admin` is deliberately
# absent: promotion to administrator is not something the user-management
# endpoint does (see backend/routes/users.py), and listing it here would make
# the omission look accidental.
ASSIGNABLE_ROLES = (Role.OBSERVER, Role.USER, Role.ANALYZER)


def assignable_role_values() -> list:
    return [r.value for r in ASSIGNABLE_ROLES]
