#!/usr/bin/env python3
"""
Complete demo-data reset
========================
Removes ALL identity-derived data - rows, face files, vector-index artifacts,
chat history - so the system restarts on an empty dataset with one storage
format: /app/storage/faces/<identity_uuid>/image_NNN.ext

THREE MODES
  --dry-run   (DEFAULT) report only. Reads nothing but counts; deletes nothing.
  --apply     destructive. Requires --yes-i-understand AND an exact typed phrase.
  --verify    read-only post-purge assertions (see --verify output).

WHAT IT REMOVES
  database  identities, identity_embeddings, identity_images,
            identity_appearances, identity_relationships, faces, detections,
            and everything that references them: audit log, merges, similarity
            training data, merge suggestions (JSONB identity ids, no FK),
            watchlist entries/alerts, live search alerts + triggers + their
            audit log, search history, the ML/risk rows derived from identities
            (predictions, shadow comparisons, assessments, signal results,
            labels, feature snapshots - whose entity_id holds identity UUIDs
            as plain strings - drift reports), the chat / SQL-agent history
            (conversations, messages, query history + embeddings, conversation
            memory/sessions, chatbot audit log), and the ML collection
            watermarks (reset so collectors re-scan rather than skipping).
  files     storage/faces (all folders + .incoming), pipeline snapshot folders
            under storage/, storage/debug/{webhook_images,cropped},
            assets/faces + assets/labeled_images (host-side if read-only),
            conversation_cache/*.json
  vectors   the FlatFaissIndex snapshot store under IDENTITY_INDEX_DB_PATH
            (CURRENT, snapshot-*/, quarantine/) plus every v1-era artifact,
            and the retired FaceDatabase's directory + the STALE PYTHON FILES
            the /app/database named volume still shadows (face_db.py,
            __init__.py, app.db) - deleting those is what makes
            `import database` genuinely fail after the code deletion.

WHAT IT PRESERVES
  users, pipelines, settings/configuration, watchlist definitions,
  ml_models / ml_datasets / ml_audit_log (provenance), learned_thresholds,
  risk_model_versions, alembic_version, and every application asset
  (icons/, tiles/, frontend/, weights/, models/).

THERE IS NO UNDO. Take a pg_dump AND scripts/backup/backup.sh first - note the
latter archives /data/storage only, so assets/ must be archived separately.

Usage:
    python scripts/purge_face_storage.py --dry-run
    python scripts/purge_face_storage.py --apply --yes-i-understand
    python scripts/purge_face_storage.py --verify
"""

import argparse
import asyncio
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIRM_PHRASE = "DELETE ALL FACE DATA"

# Deletion order. The four RESTRICT foreign keys (identity_audit_log,
# identity_merges, similarity_training_data and the self-referencing
# identities.merged_into_id) would otherwise block DELETE FROM identities, and
# merge_suggestions holds identity UUIDs in JSONB with no FK at all, so nothing
# cascades it.
DELETE_ORDER = [
    # --- identity-adjacent, RESTRICT FKs first ---
    ("identity_audit_log", "DELETE FROM identity_audit_log"),
    ("identity_merges", "DELETE FROM identity_merges"),
    ("similarity_training_data", "DELETE FROM similarity_training_data"),
    ("merge_suggestions", "DELETE FROM merge_suggestions"),
    ("identity_relationships", "DELETE FROM identity_relationships"),
    ("watchlist_alerts", "DELETE FROM watchlist_alerts"),
    ("live_alert_triggers", "DELETE FROM live_alert_triggers"),
    ("live_search_alerts", "DELETE FROM live_search_alerts"),
    ("watchlist_entries", "DELETE FROM watchlist_entries"),
    # --- history rows with no FK protection at all ---
    ("live_alert_audit_log", "DELETE FROM live_alert_audit_log"),
    ("search_history", "DELETE FROM search_history"),
    # --- ML / risk rows derived from identities.
    # ml_shadow_comparisons and risk_signal_results would cascade anyway;
    # deleting them explicitly gives the report an honest rowcount. The
    # circular FK threat_assessments.ml_prediction_id is broken by a
    # pre-UPDATE before this loop runs. ---
    ("ml_shadow_comparisons", "DELETE FROM ml_shadow_comparisons"),
    ("ml_predictions", "DELETE FROM ml_predictions"),
    ("risk_signal_results", "DELETE FROM risk_signal_results"),
    ("threat_assessments", "DELETE FROM threat_assessments"),
    ("ml_labels", "DELETE FROM ml_labels"),
    ("ml_feature_snapshots", "DELETE FROM ml_feature_snapshots"),
    ("ml_drift_reports", "DELETE FROM ml_drift_reports"),
    # --- chat / SQL-agent history: demo queries about demo people, stored
    # verbatim with no FK to identities, so no cascade ever reaches them ---
    ("message_feedback", "DELETE FROM message_feedback"),
    ("messages", "DELETE FROM messages"),
    ("conversation_branches", "DELETE FROM conversation_branches"),
    ("conversations", "DELETE FROM conversations"),
    ("user_query_embeddings", "DELETE FROM user_query_embeddings"),
    ("user_conversation_memory", "DELETE FROM user_conversation_memory"),
    ("user_query_history", "DELETE FROM user_query_history"),
    ("user_conversation_sessions", "DELETE FROM user_conversation_sessions"),
    ("chatbot_audit_log", "DELETE FROM chatbot_audit_log"),
    # --- core identity data, children before parents ---
    # Claim tickets for uploads awaiting an administrator's identity decision.
    # They FK users, not identities, but a wipe that left them behind would
    # hand out tokens pointing at files this script is about to delete.
    ("pending_enrollments", "DELETE FROM pending_enrollments"),
    ("identity_embeddings", "DELETE FROM identity_embeddings"),
    ("identity_images", "DELETE FROM identity_images"),
    ("identity_appearances", "DELETE FROM identity_appearances"),
    ("faces", "DELETE FROM faces"),
    ("detections", "DELETE FROM detections"),
    ("identities", "DELETE FROM identities"),
    # --- watermarks: collectors upsert these by collector_name on the next
    # run; leaving them would make re-collection silently skip everything
    # older than the pre-wipe watermark ---
    ("ml_collection_checkpoints", "DELETE FROM ml_collection_checkpoints"),
]

COUNT_TABLES = [table for table, _stmt in DELETE_ORDER]

PRESERVED_TABLES = ["users", "pipelines", "settings", "watchlists",
                    "ml_models", "ml_datasets", "ml_audit_log",
                    "learned_thresholds", "risk_model_versions"]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _paths():
    """Every directory in scope, resolved from configuration (never hard-coded)."""
    from config import settings

    storage = os.path.realpath(settings.STORAGE_DIR)
    faces = os.path.realpath(settings.FACES_DIR)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    webhook = os.path.realpath(
        settings.WEBHOOK_IMAGES_DIR)
    cropped = os.path.realpath(
        settings.CROPPED_IMAGES_DIR)
    return {
        "repo": repo,
        "storage": storage,
        "faces": faces,
        # The shared parent of the two debug stores. Needed as its own entry
        # because _pipeline_snapshot_dirs walks the top level of STORAGE_DIR and
        # would otherwise classify it as a pipeline directory.
        "debug": os.path.realpath(os.path.dirname(webhook)),
        # Uploads parked for an identity decision. Same reason as "debug": it
        # is a top-level directory under STORAGE_DIR with its own line item,
        # so _pipeline_snapshot_dirs must not claim it as pipeline output.
        "pending": os.path.realpath(settings.PENDING_UPLOAD_DIR),
        "assets_faces": os.path.realpath(os.path.join(repo, "assets", "faces")),
        "assets_labeled": os.path.realpath(os.path.join(repo, "assets", "labeled_images")),
        "webhook": webhook,
        "cropped": cropped,
        "faiss_identity": os.path.realpath(
            settings.IDENTITY_INDEX_DB_PATH),
        # The named volume mounted at the index path's PARENT (/app/database in
        # containers). It shadows the deleted repo `database/` package with
        # stale copies of face_db.py/__init__.py, and holds the retired
        # FaceDatabase's face_database/ directory. Derived, never hard-coded:
        # DB_PATH itself was deleted along with the code that read it.
        "legacy_db_root": os.path.realpath(
            os.path.dirname(os.path.realpath(settings.IDENTITY_INDEX_DB_PATH))),
        # ConversationMemory session JSONs (lifespan.py uses the relative
        # literal "conversation_cache"); demo chat content lives here verbatim.
        "conversation_cache": os.path.realpath(
            os.path.join(repo, "conversation_cache")),
    }


def _allowed_roots(paths):
    """Deletion can only ever reach inside these. Anything else is a bug."""
    roots = [paths["storage"], paths["webhook"], paths["cropped"],
             os.path.realpath(os.path.join(paths["repo"], "assets")),
             paths["conversation_cache"]]
    # The legacy volume root is admitted ONLY when it is a real directory that
    # is not the repository root - refusing a misconfigured
    # IDENTITY_INDEX_DB_PATH beats deleting the working tree.
    legacy = paths["legacy_db_root"]
    if os.path.isdir(legacy) and legacy not in (paths["repo"], os.path.sep):
        roots.append(legacy)
    return roots


def _assert_inside_allowed(target, paths):
    target = os.path.realpath(target)
    for root in _allowed_roots(paths):
        try:
            if os.path.commonpath([root, target]) == root:
                return
        except ValueError:
            continue
    raise SystemExit(f"REFUSING to touch {target!r} — outside the allowed roots "
                     f"{_allowed_roots(paths)}")


def _tree_stats(path):
    """(file_count, total_bytes) for a directory tree; (0, 0) when absent."""
    if not os.path.isdir(path):
        return 0, 0
    files = total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                pass
    return files, total


def _pipeline_snapshot_dirs(paths):
    """Everything under STORAGE_DIR except the directories we account for by name.

    Matched on FULL PATHS: storage/ali_abbass is a pipeline directory while
    storage/faces/ali_abbass is an identity directory, and comparing basenames
    would conflate them.

    `debug/` and `pending/` are excluded for the same reason `faces/` is: they
    live under STORAGE_DIR and are already reported and purged as their own line
    items. Without this they would be counted twice and deleted as if they were
    pipeline output.
    """
    storage = paths["storage"]
    accounted = {paths["faces"], paths["debug"], paths["pending"]}
    out = []
    if not os.path.isdir(storage):
        return out
    for entry in sorted(os.listdir(storage)):
        full = os.path.realpath(os.path.join(storage, entry))
        if full in accounted or not os.path.isdir(full):
            continue
        out.append(full)
    return out


def _faiss_artifacts(paths):
    """(files, dirs) of every vector-index artifact on disk.

    Covers BOTH generations: the v1 flat filenames, and the current
    FlatFaissIndex layout (CURRENT pointer + snapshot-NNNNNN/ + quarantine/,
    see backend/core/vector_index/flat_faiss.py). The v1-only version of this
    function silently left every snapshot behind while --verify reported clean.
    """
    files, dirs = [], []
    identity_dir = paths["faiss_identity"]

    # Legacy FaceDatabase artifacts (retired 2026-08; directory may linger in
    # the named volume).
    legacy_face_db = os.path.join(paths["legacy_db_root"], "face_database")
    if os.path.isdir(legacy_face_db):
        dirs.append(legacy_face_db)

    for name in ("known_faiss_index.bin", "unknown_faiss_index.bin",
                 "known_metadata.json", "unknown_metadata.json",
                 "CURRENT", "CURRENT.tmp"):
        candidate = os.path.join(identity_dir, name)
        if os.path.isfile(candidate):
            files.append(candidate)
    if os.path.isdir(identity_dir):
        for entry in sorted(os.listdir(identity_dir)):
            full = os.path.join(identity_dir, entry)
            if os.path.isdir(full) and (entry.startswith("snapshot-")
                                        or entry == "quarantine"):
                dirs.append(full)
    return files, dirs


def _stale_volume_files(paths):
    """Stale Python/DB files the named volume shadows over the deleted repo
    package. Removing them is what makes `import database` genuinely fail
    in-container after the code deletion."""
    files, dirs = [], []
    root = paths["legacy_db_root"]
    if not os.path.isdir(root):
        return files, dirs
    for name in ("__init__.py", "face_db.py", "app.db"):
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            files.append(candidate)
    pycache = os.path.join(root, "__pycache__")
    if os.path.isdir(pycache):
        dirs.append(pycache)
    return files, dirs


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

async def _db_counts(db):
    from sqlalchemy import text
    counts = {}
    for table in COUNT_TABLES + PRESERVED_TABLES:
        try:
            counts[table] = (await db.execute(
                text(f"SELECT count(*) FROM {table}"))).scalar() or 0
        except Exception:
            counts[table] = None      # table absent in this deployment
    return counts


def _file_inventory(paths):
    faces_dir = paths["faces"]
    name_folders, uuid_folders = [], []
    import uuid as _uuid
    if os.path.isdir(faces_dir):
        for entry in sorted(os.listdir(faces_dir)):
            full = os.path.join(faces_dir, entry)
            if not os.path.isdir(full) or entry.startswith("."):
                continue
            try:
                _uuid.UUID(entry)
                uuid_folders.append(entry)
            except (ValueError, AttributeError, TypeError):
                name_folders.append(entry)

    snapshots = _pipeline_snapshot_dirs(paths)
    snap_files = snap_bytes = 0
    for directory in snapshots:
        f, b = _tree_stats(directory)
        snap_files += f
        snap_bytes += b

    items = [
        ("enrollment files (storage/faces)", paths["faces"], *_tree_stats(paths["faces"])),
        ("assets/faces", paths["assets_faces"], *_tree_stats(paths["assets_faces"])),
        ("assets/labeled_images", paths["assets_labeled"], *_tree_stats(paths["assets_labeled"])),
        (f"pipeline snapshots ({len(snapshots)} dirs)", paths["storage"], snap_files, snap_bytes),
        ("debug crops", paths["cropped"], *_tree_stats(paths["cropped"])),
        ("webhook images", paths["webhook"], *_tree_stats(paths["webhook"])),
        ("pending uploads", paths["pending"], *_tree_stats(paths["pending"])),
    ]
    faiss_files, faiss_dirs = _faiss_artifacts(paths)
    faiss_bytes = sum(os.path.getsize(p) for p in faiss_files)
    faiss_count = len(faiss_files)
    for directory in faiss_dirs:
        f, b = _tree_stats(directory)
        faiss_count += f
        faiss_bytes += b
    items.append((f"vector-index artifacts ({len(faiss_dirs)} dirs)",
                  paths["faiss_identity"], faiss_count, faiss_bytes))

    stale_files, stale_dirs = _stale_volume_files(paths)
    stale_bytes = sum(os.path.getsize(p) for p in stale_files)
    stale_count = len(stale_files)
    for directory in stale_dirs:
        f, b = _tree_stats(directory)
        stale_count += f
        stale_bytes += b
    items.append(("stale legacy-volume files", paths["legacy_db_root"],
                  stale_count, stale_bytes))

    items.append(("conversation cache JSONs", paths["conversation_cache"],
                  *_tree_stats(paths["conversation_cache"])))
    return (items, name_folders, uuid_folders, snapshots,
            (faiss_files, faiss_dirs, stale_files, stale_dirs))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_report(counts, items, name_folders, uuid_folders, header):
    bar = "=" * 78
    print(bar)
    print(header)
    print(bar)
    print("\nDATABASE ROWS TO REMOVE")
    print(f"  {'table':<28} {'rows':>10}")
    print(f"  {'-' * 28} {'-' * 10}")
    total_rows = 0
    for table in COUNT_TABLES:
        n = counts.get(table)
        if n is None:
            print(f"  {table:<28} {'(absent)':>10}")
            continue
        total_rows += n
        print(f"  {table:<28} {n:>10}")
    print(f"  {'-' * 28} {'-' * 10}")
    print(f"  {'TOTAL ROWS':<28} {total_rows:>10}")

    print("\n  PRESERVED (never touched)")
    for table in PRESERVED_TABLES:
        n = counts.get(table)
        print(f"    {table:<26} {'(absent)' if n is None else n:>10}")

    print("\nFILES TO REMOVE")
    print(f"  {'what':<38} {'files':>8} {'size':>12}")
    print(f"  {'-' * 38} {'-' * 8} {'-' * 12}")
    total_files = total_bytes = 0
    for label, path, files, size in items:
        total_files += files
        total_bytes += size
        print(f"  {label:<38} {files:>8} {_human(size):>12}")
        print(f"      path: {path}")
    print(f"  {'-' * 38} {'-' * 8} {'-' * 12}")
    print(f"  {'TOTAL':<38} {total_files:>8} {_human(total_bytes):>12}")
    print(f"\n  TOTAL BYTES TO REMOVE: {total_bytes:,} ({_human(total_bytes)})")

    if name_folders:
        print(f"\n  Legacy display-name folders ({len(name_folders)}): "
              + ", ".join(name_folders))
    if uuid_folders:
        print(f"  UUID folders ({len(uuid_folders)}): " + ", ".join(uuid_folders))
    return total_rows, total_files, total_bytes


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

async def dry_run():
    from db_connection import db_manager

    paths = _paths()
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    async with db_manager.get_session() as db:
        counts = await _db_counts(db)
    items, name_folders, uuid_folders, _snapshots, _artifacts = _file_inventory(paths)
    _print_report(counts, items, name_folders, uuid_folders,
                  "DRY RUN — nothing has been deleted")

    blocked = [(label, path) for label, path in (
        ("storage/faces", paths["faces"]),
        ("assets/faces", paths["assets_faces"]),
        ("assets/labeled_images", paths["assets_labeled"]),
        ("debug/cropped", paths["cropped"]),
        ("debug/webhook_images", paths["webhook"]),
        ("storage/pending", paths["pending"]),
    ) if _is_read_only(path)]
    if blocked:
        print("\n  ⚠ NOT WRITABLE FROM HERE — these cannot be purged by this process")
        print("    (bind-mounted read-only; clear them from the HOST instead):")
        for label, path in blocked:
            print(f"      {label:<26} {path}")

    print("\n" + "=" * 78)
    print("Nothing was deleted. To apply, run:")
    print("  python scripts/purge_face_storage.py --apply --yes-i-understand")
    print(f'  (you will be asked to type exactly: {CONFIRM_PHRASE})')
    print("Back up FIRST — there is no undo:  scripts/backup/backup.sh")
    print("=" * 78)
    return 0


async def apply(skip_prompt=False):
    from sqlalchemy import text
    from config import settings
    from db_connection import db_manager

    if str(settings.ENVIRONMENT).strip().lower() in ("production", "prod"):
        print("REFUSING: ENVIRONMENT is production.", file=sys.stderr)
        return 2

    paths = _paths()
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    async with db_manager.get_session() as db:
        counts = await _db_counts(db)
    items, name_folders, uuid_folders, snapshots, artifacts = _file_inventory(paths)
    _print_report(counts, items, name_folders, uuid_folders,
                  "ABOUT TO DELETE — this cannot be undone")

    if not skip_prompt:
        print(f"\nType exactly  {CONFIRM_PHRASE}  to proceed (anything else aborts):")
        try:
            typed = input("> ").strip()
        except EOFError:
            typed = ""
        if typed != CONFIRM_PHRASE:
            print("Confirmation phrase did not match. ABORTED — nothing was deleted.")
            return 1

    print("\n--- deleting database rows ---")
    async with db_manager.get_session() as db:
        # Clear the self-referencing FK first or the parent delete is blocked.
        await db.execute(text("UPDATE identities SET merged_into_id = NULL "
                              "WHERE merged_into_id IS NOT NULL"))
        # Break the threat_assessments <-> ml_predictions circular FK. The
        # constraint declares SET NULL, but a database migrated before the
        # final form of a7b8c9d0e1f2 may carry it without the action - and an
        # explicit UPDATE costs nothing when the cascade would have worked.
        try:
            await db.execute(text(
                "UPDATE threat_assessments SET ml_prediction_id = NULL "
                "WHERE ml_prediction_id IS NOT NULL"))
        except Exception:
            pass  # table absent in this deployment
        for table, statement in DELETE_ORDER:
            try:
                result = await db.execute(text(statement))
                print(f"  {table:<28} {result.rowcount if result.rowcount is not None else '?':>8} deleted")
            except Exception as exc:
                print(f"  {table:<28} SKIPPED ({type(exc).__name__}: {exc})")
        await db.commit()

    print("\n--- deleting files ---")
    unwritable = []
    for label, target in (
        ("storage/faces", paths["faces"]),
        ("assets/faces", paths["assets_faces"]),
        ("assets/labeled_images", paths["assets_labeled"]),
        ("debug/cropped", paths["cropped"]),
        ("debug/webhook_images", paths["webhook"]),
        ("storage/pending", paths["pending"]),
    ):
        _assert_inside_allowed(target, paths)
        removed, failures = _empty_directory(target)
        note = ""
        if failures:
            note = f"  !! {len(failures)} FAILED — e.g. {failures[0]}"
            unwritable.append((label, target, len(failures)))
        print(f"  {label:<28} {removed:>8} entries removed{note}")

    for directory in snapshots:
        _assert_inside_allowed(directory, paths)
        shutil.rmtree(directory, ignore_errors=True)
    print(f"  {'pipeline snapshot dirs':<28} {len(snapshots):>8} removed")

    print("\n--- deleting vector-index + legacy-volume artifacts ---")
    faiss_files, faiss_dirs, stale_files, stale_dirs = artifacts
    for artifact in faiss_files + stale_files:
        _assert_inside_allowed(artifact, paths)
        try:
            os.remove(artifact)
            print(f"  removed {os.path.basename(artifact)}")
        except OSError as exc:
            print(f"  could not remove {os.path.basename(artifact)}: {exc}")
    for directory in faiss_dirs + stale_dirs:
        _assert_inside_allowed(directory, paths)
        shutil.rmtree(directory, ignore_errors=True)
        print(f"  removed {os.path.basename(directory)}/")

    print("\n--- deleting conversation cache ---")
    _assert_inside_allowed(paths["conversation_cache"], paths)
    removed, failures = _empty_directory(paths["conversation_cache"])
    print(f"  {'conversation_cache':<28} {removed:>8} entries removed"
          + (f"  !! {len(failures)} FAILED" if failures else ""))

    # Recreate the (now empty) gallery + staging area so the next upload has
    # somewhere to land and the atomic same-filesystem os.replace still works.
    os.makedirs(paths["faces"], exist_ok=True)
    os.makedirs(os.path.join(paths["faces"], ".incoming"), exist_ok=True)

    if unwritable:
        print("\n" + "!" * 78)
        print("INCOMPLETE — these targets could not be written from this process")
        print("(assets/ is bind-mounted READ-ONLY into the container, so it must be")
        print(" cleared from the HOST):")
        for label, target, count in unwritable:
            print(f"  {label:<28} {count:>6} file(s) remain at {target}")
        print("\nOn the host, from the repository root:")
        print("  rm -rf assets/faces/* assets/labeled_images/*        # bash")
        print("  Remove-Item -Recurse -Force assets\\faces\\*, assets\\labeled_images\\*   # PowerShell")
        print("!" * 78)

    print("\nDONE. Restart the API so FAISS and caches come up empty:")
    print("  docker restart face_recognition_api")
    print("Then verify:")
    print("  python scripts/purge_face_storage.py --verify")
    return 0


def _is_read_only(path):
    """True when this process cannot write into `path`.

    /app/assets is bind-mounted read-only, so a purge running inside the
    container CANNOT delete there. Silently swallowing that error made the run
    report success while leaving every file in place.
    """
    if not os.path.isdir(path):
        return False
    probe = os.path.join(path, ".purge_write_probe")
    try:
        with open(probe, "w"):
            pass
        os.remove(probe)
        return False
    except OSError:
        return True


def _empty_directory(path):
    """Remove a directory's CONTENTS, keeping the directory itself.

    Returns (removed, failures). Failures are surfaced, never swallowed.
    """
    if not os.path.isdir(path):
        return 0, []
    removed, failures = 0, []
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            removed += 1
        except OSError as exc:
            failures.append(f"{entry}: {exc.strerror or exc}")
    return removed, failures


async def verify():
    """Read-only post-purge assertions. Exit 0 only when all of them hold."""
    import uuid as _uuid
    from sqlalchemy import text
    from db_connection import db_manager

    paths = _paths()
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()

    problems = []

    async with db_manager.get_session() as db:
        counts = await _db_counts(db)
    # Every wiped table must be empty - including the ML/risk/chat tables the
    # earlier version of this script never touched.
    for table in COUNT_TABLES:
        n = counts.get(table)
        if n:
            problems.append(f"{table} still has {n} row(s)")

    for label, path in (("assets/faces", paths["assets_faces"]),
                        ("assets/labeled_images", paths["assets_labeled"]),
                        ("debug/cropped", paths["cropped"]),
                        ("debug/webhook_images", paths["webhook"]),
                        ("storage/pending", paths["pending"])):
        files, _bytes = _tree_stats(path)
        if files:
            problems.append(f"{label} still holds {files} file(s)")

    if os.path.isdir(paths["faces"]):
        for entry in sorted(os.listdir(paths["faces"])):
            if entry.startswith("."):
                continue
            full = os.path.join(paths["faces"], entry)
            if not os.path.isdir(full):
                problems.append(f"storage/faces holds a loose file: {entry}")
                continue
            try:
                _uuid.UUID(entry)
            except (ValueError, AttributeError, TypeError):
                problems.append(f"storage/faces still has a display-name folder: {entry}")

    faiss_files, faiss_dirs = _faiss_artifacts(paths)
    for artifact in faiss_files:
        size = os.path.getsize(artifact)
        # 45 bytes is an empty IndexFlatIP header; a populated index is far larger.
        if artifact.endswith(".bin") and size > 1024:
            problems.append(f"FAISS index not empty: {os.path.basename(artifact)} ({size} B)")
    for directory in faiss_dirs:
        problems.append(f"vector-index dir still present: {directory}")

    stale_files, stale_dirs = _stale_volume_files(paths)
    for stale in stale_files + stale_dirs:
        problems.append(f"stale legacy-volume artifact still present: {stale}")

    cache_files, _bytes = _tree_stats(paths["conversation_cache"])
    if cache_files:
        problems.append(f"conversation_cache still holds {cache_files} file(s)")

    async with db_manager.get_session() as db:
        head = (await db.execute(text(
            "SELECT version_num FROM alembic_version"))).scalar()
    if head != "d6e7f8a9b0c1":
        problems.append(f"alembic head is {head!r}, expected d6e7f8a9b0c1")

    async with db_manager.get_session() as db:
        orphan_images = (await db.execute(text(
            "SELECT count(*) FROM identity_embeddings e WHERE e.image_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM identity_images i WHERE i.id = e.image_id)"
        ))).scalar() or 0
        if orphan_images:
            problems.append(f"{orphan_images} embedding(s) reference a missing image")

    bar = "=" * 78
    print(bar)
    if problems:
        print(f"VERIFY FAILED — {len(problems)} problem(s)")
        print(bar)
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1
    print("VERIFY PASSED")
    print(bar)
    print("  \u2713 every wiped table (identity, ML/risk, chat history) is empty")
    print("  \u2713 ml_collection_checkpoints reset")
    print("  \u2713 assets/faces and assets/labeled_images empty")
    print("  \u2713 debug crop and webhook stores empty")
    print("  \u2713 pending-enrollment uploads empty")
    print("  \u2713 storage/faces holds only <identity_uuid>/ folders")
    print("  \u2713 vector-index snapshots and legacy FAISS artifacts gone")
    print("  \u2713 stale legacy-volume python files gone (import database now fails)")
    print("  \u2713 conversation cache empty")
    print("  \u2713 alembic head is d6e7f8a9b0c1")
    print("  \u2713 no embedding references a missing image")
    print("\n  PRESERVED (row counts)")
    for table in PRESERVED_TABLES:
        n = counts.get(table)
        print(f"    {table:<26} {'(absent)' if n is None else n:>10}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true",
                       help="(default) report what would be removed; delete nothing")
    group.add_argument("--apply", action="store_true", help="actually delete")
    group.add_argument("--verify", action="store_true",
                       help="read-only post-purge assertions")
    parser.add_argument("--yes-i-understand", action="store_true",
                        help="required with --apply")
    parser.add_argument("--assume-confirmed", action="store_true",
                        help=argparse.SUPPRESS)   # test hook; still needs --apply
    args = parser.parse_args()

    if args.verify:
        return asyncio.run(verify())
    if args.apply:
        if not args.yes_i_understand:
            print("REFUSING: --apply also requires --yes-i-understand", file=sys.stderr)
            return 2
        return asyncio.run(apply(skip_prompt=args.assume_confirmed))
    return asyncio.run(dry_run())


if __name__ == "__main__":
    raise SystemExit(main())
