"""Which map archives have had their CONTENT measured, and what the measurement said.

A tile server answering 200 proves only that bytes came back. The raster street
archive returned 200 for all 145,718 of its tiles and every one of them was the
same OpenStreetMap "Access blocked" image; every structural check passed because
every structural check measures structure. The fix is to record, per archive, the
result of actually decoding a deterministic sample of its tiles — and to treat
"never measured" as *not usable* rather than as "probably fine".

    verdict recorded and passed      -> content_ok is True   -> may be served
    verdict recorded and failed      -> content_ok is False  -> refused, with its code
    no verdict, or it describes
    different bytes than are on disk -> content_ok is None   -> refused, CONTENT_NOT_VERIFIED

The third case is the one that matters. It is what makes this fail closed: a
replaced archive is unverified *by definition*, so a stale verdict can never
authorize bytes nobody has inspected.

Identity
--------
SHA-256 of the whole file is the authoritative identity, and it is recomputed at
exactly four points: installation, boot self-healing verification, an explicit
POST /api/maps/verify, and scripts/map_data/production_gate.py. It is NEVER
computed on an availability refresh — hashing a multi-GB archive every few
minutes would be its own outage.

For the cheap per-refresh check the ledger records the full stat identity —
size, mtime_ns, ctime_ns, st_dev, st_ino — and ANY mismatch invalidates the
verdict immediately. size+mtime alone is too weak: a rebuild that happens to
produce the same length within the same mtime granularity would inherit the old
verdict, and st_ino changes on every atomic rename, which is exactly how an
archive gets replaced. st_dev/st_ino are compared only when both sides report
them non-zero — Windows bind mounts report 0 and would otherwise fail every
comparison on this dev host.

States
------
`pending` entries never authorize anything. install_dataset.py writes the
verdict for a staged archive as `pending` BEFORE the rename and flips it to
`active` after, so a crash between the two leaves new bytes on disk with no
entry that matches them: UNAVAILABLE with CONTENT_NOT_VERIFIED, never
AVAILABLE on the strength of the previous archive's verdict.
"""

import hashlib
import importlib.util
import json
import logging
import os
import tempfile
import time
from typing import Dict, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Reason codes
#
# The machine-readable vocabulary shared by the availability API, the builders
# and the install transaction. Every one of these can reach a client, so they
# are part of the wire contract: add codes, never repurpose one.
# --------------------------------------------------------------------------

CONTENT_MISSING = "CONTENT_MISSING"
CONTENT_NOT_VERIFIED = "CONTENT_NOT_VERIFIED"
PLACEHOLDER_CONTENT = "PLACEHOLDER_CONTENT"
CONTENT_DEGENERATE = "CONTENT_DEGENERATE"
CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
ARCHIVE_CORRUPT = "ARCHIVE_CORRUPT"
TILE_COUNT_INVALID = "TILE_COUNT_INVALID"
METADATA_INVALID = "METADATA_INVALID"
BUILD_INCOMPLETE = "BUILD_INCOMPLETE"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
SOURCE_BLOCKED = "SOURCE_BLOCKED"
DISK_SPACE_INSUFFICIENT = "DISK_SPACE_INSUFFICIENT"
RESOURCES_MISSING = "RESOURCES_MISSING"
MARTIN_UNREACHABLE = "MARTIN_UNREACHABLE"
PROBE_FAILED = "PROBE_FAILED"
# Internal: availability decided a style was unavailable but could not say why.
# That is a bug in this code, not a data fault — report it as such and stay
# closed rather than emitting a null reason.
AVAILABILITY_STATE_INVALID = "AVAILABILITY_STATE_INVALID"

REASON_CODES = frozenset({
    CONTENT_MISSING, CONTENT_NOT_VERIFIED, PLACEHOLDER_CONTENT, CONTENT_DEGENERATE,
    CHECKSUM_MISMATCH, ARCHIVE_CORRUPT, TILE_COUNT_INVALID, METADATA_INVALID,
    BUILD_INCOMPLETE, DOWNLOAD_FAILED, SOURCE_BLOCKED, DISK_SPACE_INSUFFICIENT,
    RESOURCES_MISSING, MARTIN_UNREACHABLE, PROBE_FAILED, AVAILABILITY_STATE_INVALID,
})

STATE_ACTIVE = "active"
STATE_PENDING = "pending"

# Human sentences for the codes that availability composes itself. Verdict
# messages from the content checker are carried verbatim instead.
CODE_TEXT = {
    CONTENT_MISSING: "dataset is not installed",
    CONTENT_NOT_VERIFIED: "dataset requires content verification",
    MARTIN_UNREACHABLE: "the tile server is not reachable",
    RESOURCES_MISSING: "a font or sprite this style needs is not served",
    PROBE_FAILED: "the dataset did not answer a tile request",
    AVAILABILITY_STATE_INVALID:
        "availability state could not be explained; treated as unavailable",
}


# --------------------------------------------------------------------------
# The content checker lives in scripts/, not in the package
# --------------------------------------------------------------------------

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COVERAGE_CHECK = os.path.join(_REPO, "scripts", "map_data", "coverage_check.py")
_checker = None


def checker():
    """scripts/map_data/coverage_check.py, loaded by path.

    It is deliberately not a package module: the same file runs inside the
    GDAL and Planetiler preparation containers, which have no backend and no
    pydantic. Loading it by path keeps ONE implementation of the content rules
    instead of a copy that drifts.
    """
    global _checker
    if _checker is None:
        spec = importlib.util.spec_from_file_location("map_coverage_check", COVERAGE_CHECK)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load the content checker from {COVERAGE_CHECK}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _checker = module
    return _checker


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def stat_identity(path: str) -> Optional[dict]:
    """The cheap identity of the bytes at `path`, or None if it is not there."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {
        "size_bytes": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": getattr(st, "st_ctime_ns", 0),
        # Bind mounts on Windows report 0 for both; recorded anyway so a Linux
        # deployment gets the stronger check without a second code path.
        "st_dev": getattr(st, "st_dev", 0) or 0,
        "st_ino": getattr(st, "st_ino", 0) or 0,
    }


def identity_matches(entry: dict, current: dict) -> bool:
    """Does `entry` describe the bytes `current` was taken from?

    Every recorded field must agree. st_dev/st_ino are skipped only when either
    side reports 0, which means the filesystem did not supply them — not that
    they matched.
    """
    if not entry or not current:
        return False
    for key in ("size_bytes", "mtime_ns", "ctime_ns"):
        if int(entry.get(key, -1)) != int(current.get(key, -2)):
            return False
    for key in ("st_dev", "st_ino"):
        recorded, live = int(entry.get(key, 0) or 0), int(current.get(key, 0) or 0)
        if recorded and live and recorded != live:
            return False
    return True


def archive_sha256(path: str) -> str:
    """Authoritative content identity. Chunked; never held in memory whole."""
    return checker().archive_sha256(path)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def ledger_path() -> str:
    return settings.MAP_CONTENT_LEDGER_PATH


def _read(path: Optional[str] = None) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """(active, pending). Absent or unreadable is ({}, {}).

    A ledger that cannot be parsed must not crash availability — it must leave
    every dataset unverified, which is the fail-closed answer anyway.
    """
    target = path or ledger_path()
    try:
        with open(target, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}, {}
    except (OSError, ValueError) as exc:
        logger.error("[MAP_LEDGER] %s is unreadable (%s); every dataset is "
                     "treated as unverified", target, exc)
        return {}, {}
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), dict):
        logger.error("[MAP_LEDGER] %s has no entries object; treating every "
                     "dataset as unverified", target)
        return {}, {}
    clean = lambda d: {k: v for k, v in (d or {}).items() if isinstance(v, dict)}  # noqa: E731
    return clean(raw.get("entries")), clean(raw.get("pending"))


def load(path: Optional[str] = None) -> Dict[str, dict]:
    """The ACTIVE verdicts, keyed by source id — the ones that can authorize."""
    return _read(path)[0]


def load_pending(path: Optional[str] = None) -> Dict[str, dict]:
    """Verdicts for staged archives that are not in place yet.

    Kept in their own map rather than alongside the active ones under the same
    key, because an install must not disturb the dataset it is replacing: a
    pending entry that overwrote the incumbent's active entry would take a
    perfectly good archive offline for the duration of the copy, and leave it
    offline if the install then crashed.
    """
    return _read(path)[1]


def save(entries: Dict[str, dict], pending: Optional[Dict[str, dict]] = None,
         path: Optional[str] = None) -> str:
    """Write the whole ledger atomically: tmp -> fsync -> replace -> fsync dir.

    A reader must never see a half-written ledger, and a crash must leave the
    previous one intact. Same discipline as backend/ml/dataset_builder.
    """
    target = path or ledger_path()
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    if pending is None:
        pending = _read(target)[1]
    payload = {
        "_comment": ("Content verdicts for map-data/production. Written by "
                     "scripts/map_data/install_dataset.py and POST /api/maps/verify. "
                     "An archive with no matching entry under `entries` is NOT usable; "
                     "`pending` holds staged archives and authorizes nothing."),
        "version": 1,
        "entries": entries,
        "pending": pending,
    }
    fd, tmp = tempfile.mkstemp(prefix=".content_verdicts.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)
    return target


def _fsync_dir(directory: str) -> None:
    """Durability of a rename needs the DIRECTORY, not just the file."""
    try:
        fd = os.open(directory, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# The runtime question
# --------------------------------------------------------------------------

def verdict_for(source_id: str, *, entries: Optional[Dict[str, dict]] = None,
                production_dir: Optional[str] = None) -> Tuple[Optional[bool], Optional[str], Optional[str]]:
    """(content_ok, code, message) for one source, from ONE os.stat.

    content_ok is True only for an active entry that passed and still describes
    the bytes on disk. False means measured and rejected. None means unverified
    — which callers must treat as not usable.
    """
    entries = load() if entries is None else entries
    prod = production_dir or settings.MAP_PRODUCTION_DIR
    path = os.path.join(prod, f"{source_id}.mbtiles")

    current = stat_identity(path)
    if current is None:
        return None, CONTENT_MISSING, f"{source_id} is not installed at {path}"

    entry = entries.get(source_id)
    if not entry:
        return None, CONTENT_NOT_VERIFIED, CODE_TEXT[CONTENT_NOT_VERIFIED]
    if entry.get("state") != STATE_ACTIVE:
        return None, CONTENT_NOT_VERIFIED, (
            f"the recorded verdict for {source_id} is {entry.get('state')!r}, "
            f"not active — an install may have been interrupted")
    if not identity_matches(entry, current):
        return None, CONTENT_NOT_VERIFIED, (
            f"{source_id} on disk is not the archive the verdict describes; "
            f"it must be re-verified")
    if entry.get("pass") is True:
        return True, None, None
    code = entry.get("code") or ARCHIVE_CORRUPT
    if code not in REASON_CODES:
        code = ARCHIVE_CORRUPT
    return False, code, entry.get("message") or f"{source_id} failed content verification"


# --------------------------------------------------------------------------
# Verification (the four places that recompute the hash)
# --------------------------------------------------------------------------

def build_entry(source_id: str, path: str, *, state: str = STATE_ACTIVE,
                verifier: str = "map_content_ledger") -> dict:
    """Measure the archive at `path` and return the entry describing it.

    Order matters: stat first, then hash, then content. A file being rewritten
    underneath us produces a mismatching entry rather than a verdict stitched
    from two different versions of the archive.
    """
    identity = stat_identity(path)
    if identity is None:
        raise FileNotFoundError(path)
    digest = archive_sha256(path)
    ok, verdict = checker().validate_archive(path)
    entry = {
        "source_id": source_id,
        "path": path,
        "state": state,
        "archive_sha256": digest,
        "kind": verdict.get("kind"),
        "pass": bool(ok),
        "code": None if ok else _code_of(verdict),
        "message": None if ok else verdict.get("reason"),
        "measured": {k: verdict.get(k) for k in
                     ("total", "checked", "distinct", "top_share", "decoded",
                      "zooms", "measured", "failures") if k in verdict},
        "verified_at": time.time(),
        "verifier": verifier,
    }
    entry.update(identity)
    return entry


def _code_of(verdict: dict) -> str:
    """The machine code for a failed verdict.

    coverage_check assigns the code at the point of refusal, so this is a
    lookup, not a guess at the wording. The fallback exists only for a verdict
    produced by an older checker.
    """
    code = verdict.get("code")
    return code if code in REASON_CODES else ARCHIVE_CORRUPT


def installed_archives(production_dir: Optional[str] = None) -> Dict[str, str]:
    """{source_id: path} for every *.mbtiles Martin would discover."""
    prod = production_dir or settings.MAP_PRODUCTION_DIR
    try:
        names = sorted(os.listdir(prod))
    except OSError:
        return {}
    return {n[: -len(".mbtiles")]: os.path.join(prod, n)
            for n in names if n.endswith(".mbtiles")}


def verify_installed(*, only_unverified: bool = False,
                     production_dir: Optional[str] = None,
                     verifier: str = "verify") -> dict:
    """Re-measure installed archives and rewrite the ledger. BLOCKING.

    Callers on the event loop must use asyncio.to_thread: this decodes tiles
    and hashes whole files.

    `only_unverified` is the boot path — an archive whose active entry still
    matches its bytes is left alone, so a restart costs one os.stat per
    dataset rather than a full re-hash.
    """
    entries = load()
    results = {}
    for source_id, path in installed_archives(production_dir).items():
        if only_unverified:
            content_ok, _code, _msg = verdict_for(
                source_id, entries=entries, production_dir=production_dir)
            if content_ok is True:
                results[source_id] = {"pass": True, "skipped": "already verified"}
                continue
        try:
            entry = build_entry(source_id, path, verifier=verifier)
        except Exception as exc:                                   # noqa: BLE001
            logger.error("[MAP_LEDGER] verification of %s raised %s: %s",
                         source_id, type(exc).__name__, exc)
            identity = stat_identity(path) or {}
            entry = {"source_id": source_id, "path": path, "state": STATE_ACTIVE,
                     "archive_sha256": None, "kind": None, "pass": False,
                     "code": ARCHIVE_CORRUPT,
                     "message": f"verification raised {type(exc).__name__}: {exc}",
                     "measured": {}, "verified_at": time.time(), "verifier": verifier}
            entry.update(identity)
        entries[source_id] = entry
        results[source_id] = {"pass": entry["pass"], "code": entry["code"],
                              "message": entry["message"], "kind": entry["kind"]}
        if entry["pass"]:
            logger.info("[MAP_LEDGER] %s verified: %s, sha256 %s", source_id,
                        entry["kind"], (entry["archive_sha256"] or "?")[:12])
        else:
            logger.error("[MAP_LEDGER] %s REJECTED (%s): %s", source_id,
                         entry["code"], entry["message"])

    # An entry for an archive that is no longer installed must not linger: if
    # a file of the same name reappears it would be judged by the stat check
    # alone, and a coincidental match is not proof of anything.
    live = set(installed_archives(production_dir))
    for stale in [k for k in entries if k not in live]:
        logger.info("[MAP_LEDGER] dropping the verdict for %s; it is not installed", stale)
        entries.pop(stale, None)

    save(entries)
    return results


def promote_pending(source_id: str, *, path: Optional[str] = None) -> bool:
    """Move a pending verdict into the active set once its bytes are in place.

    Returns False when the pending entry does not describe what is on disk —
    the interrupted-install case, which must be re-verified rather than
    rubber-stamped.
    """
    entries, pending = _read()
    entry = pending.get(source_id)
    if not entry or entry.get("state") != STATE_PENDING:
        return False
    target = path or entry.get("path") or os.path.join(
        settings.MAP_PRODUCTION_DIR, f"{source_id}.mbtiles")
    current = stat_identity(target)
    if current is None or not entry.get("pass"):
        return False
    # The pending entry was written against the STAGED file. os.replace keeps
    # the inode and mtime but updates ctime, and the recorded path differs, so
    # the cheap identity check is EXPECTED to disagree here. The hash is what
    # carries authorization across the rename — and it is recomputed from the
    # bytes now at the destination, not assumed from the staged file.
    if archive_sha256(target) != entry.get("archive_sha256"):
        return False
    entry.update(current)
    entry["state"] = STATE_ACTIVE
    entry["path"] = target
    entries[source_id] = entry
    pending.pop(source_id, None)
    save(entries, pending)
    return True


def record(entry: dict) -> None:
    """Write one verdict, preserving the rest.

    Routed by state: a pending verdict never lands in the map that authorizes
    serving, so staging a replacement cannot revoke the archive it replaces.
    """
    entries, pending = _read()
    if entry.get("state") == STATE_PENDING:
        pending[entry["source_id"]] = entry
    else:
        entries[entry["source_id"]] = entry
        pending.pop(entry["source_id"], None)
    save(entries, pending)


def drop_pending(source_id: str) -> None:
    """Forget a staged verdict whose install did not complete."""
    entries, pending = _read()
    if pending.pop(source_id, None) is not None:
        save(entries, pending)
