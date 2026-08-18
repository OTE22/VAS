#!/usr/bin/env python3
"""Shared machinery for the map dataset builders: disk, download, atomicity.

Imported by build_satellite.py and build_dem_terrarium.py, which run inside the
GDAL preparation container. That container has the repo mounted but NOT the
application: no pydantic, no backend package. So this module is stdlib-only and
must stay that way — an import of `config` here breaks every builder.

That boundary is also why free space is read with shutil.disk_usage directly
rather than through backend.core.operational_metrics.disk_capacity. Both are
the same stdlib call; the backend keeps one reader for its gauges and its API,
and a preparation script cannot import it.

Why any of this exists
----------------------
The satellite build fetched 12 Sentinel-2 COGs over GDAL's /vsicurl with no
timeout of any kind, no status check, no length check, no checksum and no
resume. It hung for roughly 16 hours, exited 1 on a COG truncated at 1,820,566
of 2,067,792 bytes, and left a 29-tile archive with an empty metadata table
sitting under the final filename — where the builder's own "refusing to
overwrite" guard then made the next run impossible without a manual rm.

Every function here is a direct answer to one of those failures.
"""

import errno
import hashlib
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# --- reason codes -----------------------------------------------------------
# A subset of backend/core/map_content_ledger.REASON_CODES. Duplicated rather
# than imported for the reason in the module docstring; tests/test_maplibre_stack.py
# asserts this stays a subset so the two cannot drift apart.
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
SOURCE_BLOCKED = "SOURCE_BLOCKED"
CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
DISK_SPACE_INSUFFICIENT = "DISK_SPACE_INSUFFICIENT"
BUILD_INCOMPLETE = "BUILD_INCOMPLETE"
ARCHIVE_CORRUPT = "ARCHIVE_CORRUPT"

# Payload prefixes that mean a server refused us and the refusal is about to be
# saved as data. Mirrors coverage_check._BLOCK_PAGE_MARKERS.
BLOCK_PAGE_MARKERS = (b"<html", b"<!doctype", b"<?xml", b"{\"error", b"{\n  \"error",
                      b"access denied", b"forbidden", b"error 403", b"rate limit")

# Content types that are never tile or raster data.
TEXTUAL_TYPES = ("text/html", "text/plain", "application/json", "application/xml",
                 "text/xml")

CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 120.0
MAX_ATTEMPTS = 5
DEFAULT_RESERVE_GB = float(os.environ.get("MAP_BUILD_DISK_RESERVE_GB", "10"))


class BuildError(Exception):
    """A refusal that names WHAT failed in a form a machine can route on."""

    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __str__(self):
        extra = " ".join(f"{k}={v}" for k, v in sorted(self.detail.items()))
        return f"[{self.code}] {self.message}" + (f" ({extra})" if extra else "")


# --- disk -------------------------------------------------------------------

def disk_free(path):
    """Free bytes on the filesystem holding `path` (walking up if needed)."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe or "/").free


def disk_preflight(path, required_bytes, *, reserve_gb=DEFAULT_RESERVE_GB, label="build"):
    """Refuse BEFORE doing any work if the volume cannot take the result.

    `required_bytes` is computed by the caller from what it is about to fetch
    and produce — never a hard-coded dataset size, so this stays honest when
    the data changes. The reserve is what must remain free afterwards, so a
    build cannot fill the disk out from under the database and the logs.

    Planetiler's equivalent guard was explicitly disabled with force=true on
    the one run that produced the vector archive; this one cannot be.
    """
    reserve = int(reserve_gb * 1024 ** 3)
    free = disk_free(path)
    required = int(required_bytes) + reserve
    if free < required:
        raise BuildError(
            DISK_SPACE_INSUFFICIENT,
            f"{label} needs {required_bytes / 1024 ** 3:.2f} GB plus a "
            f"{reserve_gb:.2f} GB reserve, but {path} has only "
            f"{free / 1024 ** 3:.2f} GB free. Nothing has been downloaded or written.",
            path=path, free_bytes=free, required_bytes=required, reserve_bytes=reserve)
    print(f"disk preflight OK: {free / 1024 ** 3:.2f} GB free, "
          f"{required / 1024 ** 3:.2f} GB required (incl. {reserve_gb:.2f} GB reserve)",
          flush=True)


def _enospc(exc):
    return isinstance(exc, OSError) and exc.errno == errno.ENOSPC


# --- durability -------------------------------------------------------------

def fsync_path(path):
    """fsync a file OR a directory. Durability of a rename needs the DIRECTORY."""
    flags = os.O_RDONLY
    if os.path.isdir(path):
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_promote(tmp, dest):
    """Put `tmp` in place as `dest` such that no reader ever sees a partial file."""
    fsync_path(tmp)
    os.replace(tmp, dest)
    fsync_path(os.path.dirname(os.path.abspath(dest)) or ".")


def sha256_file(path, chunk=4 * 1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# --- retry ------------------------------------------------------------------

def backoff_delay(attempt, base=1.0, cap=30.0):
    """Exponential backoff with FULL jitter, matching the house pattern in
    sql_agent/llm/gateway._backoff_delay.

    Full jitter rather than a fixed delay because several scene downloads
    retry at once: a constant 5 s (what GDAL_HTTP_RETRY_DELAY did) synchronises
    them into a thundering herd against the same bucket that just rate-limited
    us.
    """
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def is_transient(status=None, exc=None):
    """Retry, or stop and name the problem?

    Retrying a 403 forever is how a licence error becomes an overnight hang;
    giving up on a 503 is how a transient bucket blip fails a whole build.
    """
    if status is not None:
        if status in (408, 425, 429):
            return True
        return 500 <= status < 600
    if exc is None:
        return False
    if _enospc(exc):
        return False
    # HTTPError is a URLError subclass, so it must be classified by its status
    # BEFORE the generic network-error rule below — otherwise a 403 or a 404
    # reads as "the network flaked" and is retried until the attempts run out.
    if isinstance(exc, urllib.error.HTTPError):
        return is_transient(status=exc.code)
    return isinstance(exc, (TimeoutError, urllib.error.URLError, ConnectionError, OSError))


def status_code(status):
    """Which refusal a permanent HTTP status is."""
    return SOURCE_BLOCKED if status in (401, 403, 407, 451) else DOWNLOAD_FAILED


# --- download ---------------------------------------------------------------

def _looks_textual(content_type):
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct in TEXTUAL_TYPES


def _sniff_block_page(path, limit=512):
    try:
        with open(path, "rb") as handle:
            head = handle.read(limit).lower().lstrip()
    except OSError:
        return False
    return any(head.startswith(marker) for marker in BLOCK_PAGE_MARKERS)


def fetch(url, dest, *, expected_sha256=None, allowed_hosts=(), timeout=READ_TIMEOUT,
          connect_timeout=CONNECT_TIMEOUT, max_attempts=MAX_ATTEMPTS, validate=None,
          reserve_gb=DEFAULT_RESERVE_GB, log=print):
    """Fetch `url` to `dest`, or raise BuildError naming exactly what went wrong.

    The pipeline, in order, every step of which was missing before:

        host allow-list -> HEAD for size -> disk preflight -> stream to .part
        (resuming a previous attempt) -> HTTP status -> Content-Type ->
        Content-Length vs bytes actually written -> block-page sniff ->
        checksum when one is known -> caller validation (e.g. gdal.Open) ->
        fsync -> atomic rename

    `dest` never exists in a partial state: the file appears only after every
    check above has passed.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise BuildError(SOURCE_BLOCKED, f"refusing a non-https source: {url}", url=url)
    if allowed_hosts and parts.hostname not in allowed_hosts:
        raise BuildError(SOURCE_BLOCKED,
                         f"{parts.hostname} is not an allowed source host "
                         f"{sorted(allowed_hosts)}; refusing to download from it",
                         url=url, host=parts.hostname)

    if os.path.exists(dest) and expected_sha256:
        if sha256_file(dest) == expected_sha256:
            log(f"  have {os.path.basename(dest)} already (checksum matches)")
            return dest
        log(f"  {os.path.basename(dest)} exists but its checksum differs; refetching")
        os.unlink(dest)

    part = dest + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    total = _head_length(url, connect_timeout, log)
    if total:
        have = os.path.getsize(part) if os.path.exists(part) else 0
        disk_preflight(os.path.dirname(os.path.abspath(dest)) or ".",
                       max(0, total - have), reserve_gb=reserve_gb,
                       label=f"download of {os.path.basename(dest)}")

    last = None
    for attempt in range(max_attempts):
        if attempt:
            delay = backoff_delay(attempt)
            log(f"  retry {attempt}/{max_attempts - 1} for {os.path.basename(dest)} "
                f"in {delay:.1f}s ({last})")
            time.sleep(delay)
        try:
            written = _stream(url, part, total, connect_timeout, timeout, log)
        except BuildError:
            raise                                   # already classified; do not retry
        except Exception as exc:                    # noqa: BLE001
            if _enospc(exc):
                _discard(part)
                raise BuildError(DISK_SPACE_INSUFFICIENT,
                                 f"ran out of space writing {part}; the partial file was "
                                 f"removed and {dest} was never created",
                                 url=url, path=part) from exc
            last = f"{type(exc).__name__}: {exc}"
            if not is_transient(exc=exc):
                _discard(part)
                raise BuildError(DOWNLOAD_FAILED, f"{url} failed permanently: {last}",
                                 url=url) from exc
            continue                                # keep .part; resume next attempt

        if total and written != total:
            last = f"got {written} of {total} bytes"
            log(f"  short read: {last}")
            continue                                # resume rather than restart

        if _sniff_block_page(part):
            _discard(part)
            raise BuildError(SOURCE_BLOCKED,
                             f"{url} returned an error/deny page, not data — the first "
                             f"bytes are markup. Nothing was saved.", url=url)

        if expected_sha256:
            digest = sha256_file(part)
            if digest != expected_sha256:
                _discard(part)
                raise BuildError(CHECKSUM_MISMATCH,
                                 f"{url} downloaded completely but its sha256 is {digest}, "
                                 f"not the expected {expected_sha256}",
                                 url=url, expected=expected_sha256, actual=digest)

        if validate is not None:
            try:
                validate(part)
            except Exception as exc:                # noqa: BLE001
                _discard(part)
                raise BuildError(ARCHIVE_CORRUPT,
                                 f"{url} downloaded but does not open as the expected "
                                 f"format: {type(exc).__name__}: {exc}", url=url) from exc

        atomic_promote(part, dest)
        log(f"  ok {os.path.basename(dest)} ({os.path.getsize(dest)} bytes)")
        return dest

    _discard(part)
    raise BuildError(DOWNLOAD_FAILED,
                     f"{url} failed after {max_attempts} attempts: {last}. "
                     f"{dest} was not created.", url=url, attempts=max_attempts)


def _discard(part):
    try:
        os.unlink(part)
    except OSError:
        pass


def head_length(url, connect_timeout=CONNECT_TIMEOUT, log=print):
    """Content-Length from a HEAD, or None. Never fatal — some hosts refuse HEAD.

    Public because a builder whose manifest records no sizes has to ask the
    server before it can preflight the disk: a size check that silently
    computes zero is worse than none, since it reads as protection.
    """
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=connect_timeout) as resp:
            return int(resp.headers.get("Content-Length") or 0) or None
    except Exception as exc:                                       # noqa: BLE001
        log(f"  HEAD {url} did not answer a length ({type(exc).__name__}); "
            f"size checks fall back to the streamed body")
        return None


def _head_length(url, connect_timeout, log):
    return head_length(url, connect_timeout, log)


def _stream(url, part, total, connect_timeout, timeout, log):
    """One attempt. Resumes `part` with Range when the server allows it.

    Returns the size of `part` afterwards.

    The timeout passed to urlopen is a SOCKET timeout, so it bounds every
    read() on the body, not just the handshake. That is the property the old
    /vsicurl path lacked: a connection that opens and then stalls produced a
    16-hour hang instead of a failure.
    """
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if total and have >= total:
        return have
    request = urllib.request.Request(url)
    if have:
        request.add_header("Range", f"bytes={have}-")
    try:
        resp = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # urlopen RAISES on 4xx/5xx rather than returning them, so the status
        # has to be classified here; letting it fall through to the generic
        # network-error handler retried permanent refusals until the attempt
        # budget ran out and then reported the wrong code.
        if exc.code == 416:                     # range not satisfiable = complete
            return have
        if is_transient(status=exc.code):
            raise ConnectionError(f"HTTP {exc.code}") from exc
        raise BuildError(status_code(exc.code),
                         f"{url} returned HTTP {exc.code}; it will not be retried",
                         url=url, status=exc.code) from exc
    with resp:
        status = getattr(resp, "status", resp.getcode())
        ctype = resp.headers.get("Content-Type")
        if _looks_textual(ctype):
            raise BuildError(SOURCE_BLOCKED,
                             f"{url} answered with Content-Type {ctype!r} — that is an "
                             f"error page, not data", url=url, content_type=ctype)
        # 206 means the server honoured the Range and is sending the remainder;
        # 200 means it ignored it and is resending everything, so the partial
        # file must be truncated or the two copies would be concatenated.
        mode = "ab" if (status == 206 and have) else "wb"
        if mode == "wb":
            have = 0
        # The length THIS response promises. Taken from the GET rather than
        # only from the earlier HEAD, because a host that refuses HEAD still
        # answers one here — and without any declared length a connection cut
        # halfway produces a short file that looks complete. That is precisely
        # how the satellite build ingested a COG truncated at 1,820,566 of
        # 2,067,792 bytes.
        declared = None
        try:
            declared = int(resp.headers.get("Content-Length"))
        except (TypeError, ValueError):
            declared = None
        expected_here = declared + have if declared is not None else None

        written = 0
        with open(part, mode) as handle:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        size = os.path.getsize(part)
        if expected_here is not None and size != expected_here:
            # Transient by nature — a cut connection. Raising here (rather than
            # returning a short size) keeps the .part for the next attempt to
            # resume from.
            raise ConnectionError(
                f"connection closed after {written} of {declared} declared bytes "
                f"({size} of {expected_here} total)")
        return size
    return os.path.getsize(part)


# --- subprocesses -----------------------------------------------------------

def run(cmd, *, timeout=3600, env=None, log=print):
    """Run a child process with a TIMEOUT and an explicit environment.

    Both matter: gdal.SetConfigOption is in-process only, so the twelve
    `gdalwarp` children of the old satellite builder inherited none of its HTTP
    retry configuration, and none of them could ever time out.
    """
    log("+ " + " ".join(cmd))
    child_env = dict(os.environ)
    if env:
        child_env.update(env)
    try:
        subprocess.run(cmd, check=True, timeout=timeout, env=child_env)
    except subprocess.TimeoutExpired as exc:
        raise BuildError(DOWNLOAD_FAILED,
                         f"{cmd[0]} exceeded its {timeout}s timeout and was killed",
                         command=" ".join(cmd)) from exc
    except subprocess.CalledProcessError as exc:
        raise BuildError(BUILD_INCOMPLETE,
                         f"{cmd[0]} exited {exc.returncode}",
                         command=" ".join(cmd), returncode=exc.returncode) from exc


# --- tiling (one implementation; both raster builders duplicated these) -----

def lonlat_to_tile(lon, lat, z):
    """WGS84 -> XYZ tile indices."""
    import math
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(x, n - 1)), max(0, min(y, n - 1))


def tile_bounds_3857(z, x, y):
    """Web-Mercator metre bounds of one XYZ tile."""
    half = 20037508.342789244
    size = 2 * half / (2 ** z)
    minx = -half + x * size
    maxy = half - y * size
    return minx, maxy - size, minx + size, maxy


def main():
    """Self-check, so the module can be smoke-tested inside a prep container."""
    print(f"disk free here: {disk_free('.') / 1024 ** 3:.2f} GB")
    print(f"backoff delays: {[round(backoff_delay(i), 2) for i in range(5)]}")
    print(f"transient 503={is_transient(status=503)} 403={is_transient(status=403)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
