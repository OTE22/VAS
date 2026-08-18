"""What exactly is running — so repository-vs-runtime drift is visible.

This deployment has already been bitten twice by the runtime silently
diverging from the repository: a container held an env value three days
older than the compose file, and an image predated the Dockerfile step it
was assumed to contain. Both were invisible because nothing in the running
system stated its own provenance.

The fingerprint is logged once at startup and returned by /health/detailed,
so "what version/commit/config is this?" is answerable from the log or one
curl — without docker inspect, and without guessing.

Never put secrets here: it is written to the log and served on an
unauthenticated health endpoint. Database credentials are stripped; only
host, port and database name survive.
"""

import os.path
import subprocess
from urllib.parse import urlsplit

from config import settings


def _git_commit() -> str:
    """Best-effort commit id, without requiring git in the image.

    Priority: an explicitly injected GIT_COMMIT (the reliable path for baked
    production images), then reading .git directly (works in the dev stack,
    which bind-mounts the repository), then the git binary, then "unknown".

    GIT_COMMIT comes from settings, not os.environ: config.py is the one
    module allowed to read the environment, and a second reader would be a
    second source of truth — exactly what this module exists to detect.
    """
    injected = (settings.GIT_COMMIT or "").strip()
    if injected:
        return injected

    git_dir = "/app/.git"
    try:
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as handle:
            head = handle.read().strip()
        if head.startswith("ref:"):
            ref = head.split(None, 1)[1]
            ref_path = os.path.join(git_dir, *ref.split("/"))
            if os.path.exists(ref_path):
                with open(ref_path, encoding="utf-8") as handle:
                    return handle.read().strip()[:12]
            # packed refs
            with open(os.path.join(git_dir, "packed-refs"), encoding="utf-8") as handle:
                for line in handle:
                    if line.strip().endswith(ref):
                        return line.split()[0][:12]
        else:
            return head[:12]  # detached HEAD
    except OSError:
        pass

    try:
        return subprocess.run(
            ["git", "-C", "/app", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, timeout=5,
        ).stdout.decode().strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _database_target() -> str:
    """host:port/dbname — never the credentials."""
    try:
        parts = urlsplit(settings.DATABASE_URL)
        host = parts.hostname or "?"
        port = parts.port or 5432
        name = (parts.path or "/?").lstrip("/")
        return f"{host}:{port}/{name}"
    except (ValueError, AttributeError):
        return "unparseable"


def build_fingerprint() -> dict:
    """Every value comes from settings — no getattr fallbacks.

    A `getattr(settings, "X", "unknown")` here would re-declare a default that
    config.py already owns, so a renamed or removed setting would report
    "unknown" forever instead of failing loudly. These are all declared
    settings; if one disappears, this should break.
    """
    return {
        "version": settings.VERSION,
        "git_commit": _git_commit(),
        "environment": settings.ENVIRONMENT,
        # Docker sets HOSTNAME to the container's short id, which is what maps
        # a log line back to `docker ps`. NOT the image digest — that still
        # needs `docker inspect` on the host.
        "container": settings.HOSTNAME or "unknown",
        "vector_backend": settings.VECTOR_BACKEND,
        "database": _database_target(),
        "migrations_mode": settings.MIGRATIONS_MODE,
        "expected_migration_head": settings.MIGRATIONS_EXPECTED_HEAD or "unpinned",
        "workers": settings.WORKERS,
        "gpu": bool(settings.USE_GPU),
    }


def log_fingerprint(logger) -> dict:
    """One greppable block at startup. Returns the dict for reuse."""
    fingerprint = build_fingerprint()
    logger.info("🔎 Runtime fingerprint: " + " | ".join(
        f"{key}={value}" for key, value in fingerprint.items()))
    return fingerprint
