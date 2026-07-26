"""
Password Hashing Utilities
==========================

bcrypt is intentionally slow (~200-300ms). These functions are synchronous —
async callers MUST run them in an executor (see auth_service.py) so a login
never blocks the event loop.
"""

import bcrypt
import logging

logger = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a bcrypt hash"""
    if not password or not password_hash:
        return False

    if not password_hash.startswith(('$2a$', '$2b$', '$2y$')):
        logger.error("[PASSWORD] Invalid stored hash format (not a bcrypt hash)")
        return False

    try:
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
    except (ValueError, TypeError) as e:
        logger.error(f"[PASSWORD] Verification error: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        logger.error(f"[PASSWORD] Unexpected verification error: {type(e).__name__}", exc_info=True)
        return False


if __name__ == "__main__":
    test_hash = hash_password("example")
    print(test_hash)
    print(verify_password("example", test_hash))
