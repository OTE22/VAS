"""Docker-secret file resolution.

Stdlib only, and no import of `config` — this module is called from inside
`Settings.__init__`, so any import of the settings object would be circular.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def read_secret_file(path: str) -> Optional[str]:
    """Read a Docker secret from disk.

    Strips exactly one trailing newline — the one an editor or `echo` adds —
    while preserving any other whitespace, because a generated password may
    legitimately end in a space or a tab.

    Returns None when the file is missing, unreadable, or empty.
    """
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        # The path is operator-supplied configuration, not a secret.
        logger.error("Secret file %s could not be read: %s", path, type(exc).__name__)
        return None

    if content.endswith("\r\n"):
        content = content[:-2]
    elif content.endswith("\n"):
        content = content[:-1]

    return content or None


def resolve_secret(
    value: Optional[str],
    file_path: Optional[str],
    *,
    name: str,
) -> Optional[str]:
    """Resolve a secret from a file path in preference to an inline value.

    `name` is used for logging only and must be the setting NAME, never its
    value — nothing in this function may log the secret itself.
    """
    from_file = read_secret_file(file_path) if file_path else None

    if from_file is not None:
        if value:
            logger.warning(
                "%s is set both inline and via a secret file; the file wins", name
            )
        return from_file

    if file_path and from_file is None:
        logger.error(
            "%s_FILE points at %s but no usable secret was read from it",
            name,
            file_path,
        )

    return value or None


def secret_file_mode_is_safe(path: str) -> bool:
    """True when the secret file is not readable by group or other.

    Always True on Windows, where POSIX mode bits are not meaningful.
    """
    if not path or os.name == "nt":
        return True
    try:
        return not (os.stat(path).st_mode & 0o077)
    except OSError:
        return False
