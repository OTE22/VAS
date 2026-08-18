#!/usr/bin/env python3
"""Install a built map archive as one crash-safe transaction.

    docker exec face_recognition_api python3 /app/scripts/map_data/install_dataset.py \
        /app/map-data/production/lebanon-satellite.mbtiles.new lebanon-satellite

Run through scripts/map_data/install_dataset.sh, which is the operator entry
point and adds the martin restart. This module holds the transaction because a
shell script cannot be fault-injected at each step, and every step here has a
crash window that must be proven safe.

The property being protected
----------------------------
Replacing a dataset means new bytes appear at a path whose previous contents
were authorized by a content verdict. If authorization survives the swap, the
new bytes inherit permission nobody granted them — which is precisely how an
archive of 145,718 "Access blocked" images was served as a street map.

So the transaction is ordered such that EVERY crash point leaves one of two
states, and never a third:

    step                                   crash here leaves
    1  stage to <dest>.staged              old archive serving, AVAILABLE
    2  validate + hash the staged file     old archive serving, AVAILABLE
    3  write a PENDING verdict             old archive serving, AVAILABLE
    4  fsync; retain old as <dest>.previous  old archive serving, AVAILABLE
    5  os.replace(staged, dest)            new bytes, UNAVAILABLE (see below)
    6  flip PENDING -> ACTIVE, catalogs    new bytes, UNAVAILABLE
    7  refresh; drop <dest>.previous       new archive serving, AVAILABLE

The safety at step 5 is not a special case in the code — it falls out of
binding every verdict to a file identity. After the rename, the ACTIVE entry
still describes the OLD file's size/mtime/inode, so it no longer matches what
is on disk, so verdict_for() returns CONTENT_NOT_VERIFIED and availability
reports the dataset unavailable. There is no state in which partially
committed replacement bytes are AVAILABLE.

A crash between 5 and 6 is recoverable without operator action: the pending
verdict names the new archive's sha256, and boot verification re-measures any
archive lacking an active verdict. `--rollback` restores <dest>.previous for an
operator who would rather go back than forward.

Fault injection
---------------
MAP_INSTALL_FAIL_AT=<point> aborts immediately after that step, for the tests
that prove the table above. Points: after_stage, after_validate,
after_ledger_pending, before_rename, after_rename, before_ledger_active,
after_ledger_active, before_refresh.
"""

import argparse
import errno
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/app")

from backend.core import map_content_ledger as ledger        # noqa: E402
from backend.core.operational_metrics import disk_capacity   # noqa: E402
from config import settings                                  # noqa: E402

# A test-only fault-injection seam, NOT an application setting: it exists so
# the crash-safety of the install transaction can be proven at each step, and
# it is deliberately kept out of config.py so it can never be set through the
# admin settings API. The disk reserve, which IS a setting, comes from config.
FAIL_AT = os.environ.get("MAP_INSTALL_FAIL_AT", "")
DEFAULT_RESERVE_GB = float(settings.MAP_INSTALL_DISK_RESERVE_GB)


class InstallError(Exception):
    """A refusal with a machine-readable code, in the shared registry."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _checkpoint(name):
    """Abort here when the tests ask for it. A no-op in production."""
    if FAIL_AT and FAIL_AT == name:
        print(f"FAULT INJECTION: aborting at {name}", flush=True)
        os._exit(90)


def _fsync_dir(directory):
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


def _fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def preflight_disk(dest_dir, needed_bytes, reserve_gb=DEFAULT_RESERVE_GB):
    """Refuse before copying anything if the volume cannot take it.

    Sized from the artifact in hand rather than a constant: no dataset size is
    hard-coded anywhere, so this stays correct when the archives change.
    """
    reserve = int(reserve_gb * 1024 ** 3)
    capacity = disk_capacity(dest_dir)
    if capacity is None:
        print(f"WARNING: cannot read free space on {dest_dir}; proceeding", flush=True)
        return
    _total, _used, free = capacity
    required = needed_bytes + reserve
    if free < required:
        raise InstallError(
            ledger.DISK_SPACE_INSUFFICIENT,
            f"{dest_dir} has {free / 1024 ** 3:.2f} GB free; this install needs "
            f"{needed_bytes / 1024 ** 3:.2f} GB plus a {reserve_gb:.2f} GB reserve "
            f"({required / 1024 ** 3:.2f} GB). Free space or lower "
            f"MAP_INSTALL_DISK_RESERVE_GB; nothing has been changed.")


def _copy(src, dst):
    """Copy with ENOSPC surfaced as the registered code, not a stray OSError."""
    try:
        shutil.copyfile(src, dst)
    except OSError as exc:
        try:
            os.unlink(dst)
        except OSError:
            pass
        if exc.errno == errno.ENOSPC:
            raise InstallError(ledger.DISK_SPACE_INSUFFICIENT,
                               f"ran out of space writing {dst}; the staged copy was "
                               f"removed and the installed archive is untouched") from exc
        raise


def _retain_previous(dest, previous):
    """Keep the archive currently in place until the new one is committed.

    Deliberately a COPY and not a hard link, even though a link is free.
    os.link bumps the inode's ctime, ctime is part of the identity that
    authorizes an archive, and so linking the incumbent aside would revoke its
    own verdict — taking a working dataset offline for the duration of an
    install that has not touched it. Measured: with a hard link here, a crash
    before the rename left the OLD archive on disk reporting
    CONTENT_NOT_VERIFIED. Reading the file to copy it changes no metadata that
    the verdict depends on.

    The extra space is accounted for in preflight_disk (size * 2).
    """
    if not os.path.exists(dest):
        return False
    if os.path.exists(previous):
        os.unlink(previous)
    _copy(dest, previous)
    return True


def _update_checksums(dataset_id, digest, metadata_dir):
    """Rewrite the one line for this dataset, atomically."""
    path = os.path.join(metadata_dir, "checksums.txt")
    keep = []
    try:
        with open(path, encoding="utf-8") as handle:
            keep = [line for line in handle.read().splitlines()
                    if line.strip() and not line.endswith(f"  {dataset_id}.mbtiles")]
    except FileNotFoundError:
        pass
    keep.append(f"{digest}  {dataset_id}.mbtiles")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(sorted(keep)) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(metadata_dir)


def _update_datasets(dataset_id, dest, digest, verdict, metadata_dir):
    """Upsert this dataset's provenance record, preserving curated fields.

    Only the measured facts are overwritten. provider/licence/attribution are
    written by a human and must survive a reinstall.
    """
    path = os.path.join(metadata_dir, "datasets.json")
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        doc = {}
    datasets = doc.get("datasets")
    if not isinstance(datasets, list):
        datasets = []
    entry = next((d for d in datasets if d.get("id") == dataset_id), None)
    if entry is None:
        entry = {"id": dataset_id}
        datasets.append(entry)
    zooms = sorted(int(z) for z in (verdict.get("zooms") or {}))
    entry.update({
        "size_bytes": os.path.getsize(dest),
        "sha256": digest,
        "tile_count": verdict.get("total"),
        "tile_kind": verdict.get("kind"),
        "install_date": time.strftime("%Y-%m-%d"),
        "martin_source_id": dataset_id,
        "content_verified": True,
    })
    if zooms:
        entry["minzoom"], entry["maxzoom"] = zooms[0], zooms[-1]
    doc["datasets"] = sorted(datasets, key=lambda d: d.get("id") or "")
    doc.setdefault("_comment",
                   "One entry per production archive. Written by "
                   "scripts/map_data/install_dataset.py; content verdicts live in "
                   "content_verdicts.json. Source data (map-data/source/) stays on "
                   "the preparation machine and is not transferred.")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(doc, handle, indent=1, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(metadata_dir)


def install(source, dataset_id, *, production_dir=None, metadata_dir=None,
            reserve_gb=DEFAULT_RESERVE_GB):
    """Run the transaction. Returns the committed ledger entry."""
    production = production_dir or settings.MAP_PRODUCTION_DIR
    metadata = metadata_dir or settings.MAP_METADATA_DIR
    dest = os.path.join(production, f"{dataset_id}.mbtiles")
    staged = f"{dest}.staged"
    previous = f"{dest}.previous"

    if not os.path.isfile(source):
        raise InstallError(ledger.CONTENT_MISSING, f"no such archive: {source}")
    os.makedirs(production, exist_ok=True)
    os.makedirs(metadata, exist_ok=True)

    # The archive currently in place, and whether it is authorized. A verified
    # archive is never replaced by one that has not passed — the whole point of
    # staging is that the new file must earn its place first.
    incumbent_ok, _code, _msg = ledger.verdict_for(dataset_id, production_dir=production)
    if incumbent_ok is True:
        print(f"note: {dataset_id} is currently installed and content-verified; "
              f"it stays in place unless the new archive passes", flush=True)

    # Leftovers from an install that died partway. The retained copy is only
    # useful until the current archive is verified again; keeping it forever
    # doubles the footprint of every dataset on the volume.
    if os.path.exists(previous) and incumbent_ok is True:
        os.unlink(previous)
        print(f"removed a stale {os.path.basename(previous)} from an earlier run", flush=True)

    # --- 1. stage ---------------------------------------------------------
    size = os.path.getsize(source)
    # Room for the staged copy AND the retained previous version.
    preflight_disk(production, size * 2, reserve_gb)
    if os.path.exists(staged):
        os.unlink(staged)
    print(f"staging {source} -> {staged} ({size / 1024 ** 2:.1f} MB)", flush=True)
    _copy(source, staged)
    _fsync_file(staged)
    _checkpoint("after_stage")

    try:
        # --- 2. validate + hash -------------------------------------------
        print("validating the staged archive (structure + content)...", flush=True)
        ok, verdict = ledger.checker().validate_archive(staged)
        if not ok:
            code = verdict.get("code") or ledger.ARCHIVE_CORRUPT
            raise InstallError(code, f"REFUSING to install {dataset_id}: "
                                     f"{verdict.get('reason')}")
        digest = ledger.archive_sha256(staged)
        print(f"  PASS {verdict.get('kind')}: {verdict.get('total')} tiles, "
              f"{verdict.get('distinct')} distinct, top share {verdict.get('top_share')}, "
              f"sha256 {digest[:16]}…", flush=True)
        _checkpoint("after_validate")

        # --- 3. pending verdict, BEFORE the bytes move --------------------
        entry = ledger.build_entry(dataset_id, staged, state=ledger.STATE_PENDING,
                                   verifier="install_dataset")
        entry["path"] = dest                       # where it is about to live
        ledger.record(entry)
        _checkpoint("after_ledger_pending")

        # --- 4. durability + keep the incumbent ---------------------------
        _fsync_file(staged)
        _fsync_dir(production)
        had_previous = _retain_previous(dest, previous)
        _checkpoint("before_rename")

        # --- 5. the swap ---------------------------------------------------
        os.replace(staged, dest)
        _fsync_dir(production)
        print(f"installed {dest}", flush=True)
        _checkpoint("after_rename")

        # --- 6. authorize the new bytes ------------------------------------
        _checkpoint("before_ledger_active")
        if not ledger.promote_pending(dataset_id, path=dest):
            raise InstallError(
                ledger.CONTENT_NOT_VERIFIED,
                f"{dataset_id} is in place but its verdict could not be activated; "
                f"it is reported UNAVAILABLE. Re-run verification, or use --rollback "
                f"to restore the previous archive.")
        _update_checksums(dataset_id, digest, metadata)
        _update_datasets(dataset_id, dest, digest, verdict, metadata)
        _checkpoint("after_ledger_active")

        # --- 7. commit ------------------------------------------------------
        _checkpoint("before_refresh")
        if had_previous and os.path.exists(previous):
            os.unlink(previous)
        _fsync_dir(production)
        return ledger.load().get(dataset_id)
    except BaseException:
        # Anything that fails BEFORE the rename must leave no trace. After the
        # rename the archive stays (it is valid; it is just not authorized yet)
        # and the operator is told how to finish or undo.
        if os.path.exists(staged):
            try:
                os.unlink(staged)
                print("removed the staged copy; the installed archive is untouched",
                      flush=True)
            except OSError:
                pass
            # The staged bytes are gone, so the verdict describing them must go
            # too — otherwise a later archive of the same size and mtime could
            # be promoted against a verdict taken from different data.
            try:
                ledger.drop_pending(dataset_id)
            except Exception:                                      # noqa: BLE001
                pass
        raise


def rollback(dataset_id, *, production_dir=None):
    """Put <dest>.previous back and re-verify it. The undo for a half-install."""
    production = production_dir or settings.MAP_PRODUCTION_DIR
    dest = os.path.join(production, f"{dataset_id}.mbtiles")
    previous = f"{dest}.previous"
    if not os.path.exists(previous):
        raise InstallError(ledger.CONTENT_MISSING,
                           f"no retained previous archive at {previous}")
    os.replace(previous, dest)
    _fsync_dir(production)
    entry = ledger.build_entry(dataset_id, dest, verifier="rollback")
    ledger.record(entry)
    print(f"rolled {dataset_id} back; content_ok={entry['pass']}", flush=True)
    return entry


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source", nargs="?", help="the built archive (*.mbtiles.new)")
    ap.add_argument("dataset_id", nargs="?", help="martin source id, e.g. lebanon-satellite")
    ap.add_argument("--rollback", metavar="DATASET_ID",
                    help="restore the retained previous archive for this dataset")
    ap.add_argument("--disk-reserve-gb", type=float, default=DEFAULT_RESERVE_GB,
                    help=f"free space to keep beyond this install (default {DEFAULT_RESERVE_GB})")
    args = ap.parse_args()

    try:
        if args.rollback:
            rollback(args.rollback)
            return 0
        if not args.source or not args.dataset_id:
            ap.error("source and dataset_id are required unless --rollback is given")
        entry = install(args.source, args.dataset_id, reserve_gb=args.disk_reserve_gb)
        print(f"OK {args.dataset_id}: state={entry['state']} pass={entry['pass']} "
              f"kind={entry['kind']} sha256={entry['archive_sha256'][:16]}…")
        return 0
    except InstallError as exc:
        print(f"FAILED [{exc.code}] {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
