#!/usr/bin/env python
"""Consolidate identities that are the same person, and repair what they broke.

    docker exec face_recognition_api python scripts/repair_duplicate_identities.py
    docker exec face_recognition_api python scripts/repair_duplicate_identities.py --apply --yes-i-understand
    docker exec face_recognition_api python scripts/repair_duplicate_identities.py --verify

WHY THIS EXISTS
---------------
An upload was reported as showing "an unrelated image at 100%". The similarity
maths turned out to be correct — measured against the live database, an
identical file scores 1.000000 and unrelated faces score 0.04-0.13. The real
cause was in the DATA: the same face had been enrolled under many different
names, so a search legitimately returned several identities at 1.0, and the
names attached to them looked unrelated to the operator.

Two mechanisms produced that state, both now fixed in code:

  * `identity_service` replaced an identity's `best_snapshot_path` whenever
    `similarity > 0.0` — true for essentially every match — so an identity's
    displayed face became whatever arrived last. A CORRECT match could show a
    different person's photo.
  * auto-enrichment wrote runtime observations into a matched identity at an
    effective attach floor of 0.30, with no provenance flag.

Fixing the code stops new damage. This repairs what already happened.

WHAT IT DOES NOT DO
-------------------
It does not guess. Identities are consolidated only on EXACT evidence:
byte-identical embeddings, or byte-identical image checksums. Two different
photos of the same person are NOT merged by this script — that is a judgement
call, and `merge_suggestions` plus human review already exist for it.

SAFETY
------
Dry-run is the default and touches nothing. `--apply` additionally requires
`--yes-i-understand`. Every deletion is inside a transaction, every file
operation is containment-checked against FACES_DIR, and the whole run is
idempotent: a second dry-run after a successful apply reports zero changes.
Automated actions are audited under the `system` principal, never a human.
"""

import argparse
import asyncio
import hashlib
import os
import sys
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

CONFIRM_PHRASE = "consolidate duplicate identities"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _vector(raw):
    """pgvector returns its value as a string over raw SQL; parse to ndarray."""
    import numpy as np

    if raw is None:
        return None
    if isinstance(raw, str):
        return np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)
    return np.asarray(raw, dtype=np.float32).reshape(-1)


def _signature(vector) -> str:
    """Stable identity of an exact vector. Byte-level, not approximate."""
    return hashlib.sha256(vector.tobytes()).hexdigest()


def _faces_dir():
    from config import settings
    return os.path.realpath(settings.FACES_DIR)


def _assert_inside_faces(path: str) -> str:
    """Refuse to touch anything outside FACES_DIR. Symlink-resistant."""
    root = _faces_dir()
    target = os.path.realpath(path)
    if os.path.commonpath([root, target]) != root:
        raise SystemExit(f"REFUSING to touch a path outside FACES_DIR: {target}")
    return target


# NOTE: an earlier draft grouped identities with union-find, linking them
# transitively (A shares a vector with B, B shares a different vector with C,
# therefore {A,B,C}). On this database that proposed folding thirteen
# identities together, including two whose faces score -0.08 against each
# other, because one member was contaminated and transitivity spread it. The
# union-find is deliberately gone: see build_plan, where every group is
# anchored to a single exact vector instead.


# ---------------------------------------------------------------------------
# analysis (read-only)
# ---------------------------------------------------------------------------

async def _load_state(db):
    """Everything the plan needs, in three queries. No writes."""
    from sqlalchemy import text

    identities = {}
    for row in (await db.execute(text(
            "SELECT id::text, display_name, status::text, type::text, created_at, "
            "       best_snapshot_path "
            "FROM identities"))).all():
        identities[row[0]] = {
            "id": row[0], "display_name": row[1], "status": row[2],
            "type": row[3], "created_at": row[4], "best_snapshot_path": row[5],
        }

    embeddings = []
    for row in (await db.execute(text(
            "SELECT id, identity_id::text, embedding, vector_index_sync_state "
            "FROM identity_embeddings"))).all():
        embeddings.append({
            "id": row[0], "identity_id": row[1], "vector": _vector(row[2]),
            "sync_state": row[3],
        })

    images = []
    for row in (await db.execute(text(
            "SELECT id, identity_id::text, file_checksum, storage_path, is_primary "
            "FROM identity_images"))).all():
        images.append({
            "id": row[0], "identity_id": row[1], "checksum": row[2],
            "storage_path": row[3], "is_primary": row[4],
        })

    return identities, embeddings, images


def _degenerate(embeddings):
    """Rows whose vector cannot participate in a cosine comparison.

    A zero-magnitude vector makes `<=>` return NaN, and PostgreSQL orders NaN
    ABOVE every real number, so such a row passed every threshold in every
    search until the query guards were added.
    """
    import numpy as np

    bad = []
    for row in embeddings:
        vector = row["vector"]
        if vector is None or vector.size == 0:
            bad.append((row, "null/empty"))
            continue
        if not np.all(np.isfinite(vector)):
            bad.append((row, "non-finite"))
            continue
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            bad.append((row, "zero-norm"))
        elif abs(norm - 1.0) > 0.01:
            bad.append((row, f"norm={norm:.4f}"))
    return bad


def _embed_stored_file(storage_path):
    """The face in a stored file, or None if it cannot be read or embedded.

    Used only to settle a contested file: two identities hold the same bytes but
    their faces disagree, so the bytes have to speak for themselves.
    """
    try:
        from backend.core import model_manager
        from backend.core.enrollment_service import prepare_upload

        model_manager.initialize()
        full = os.path.join(os.path.dirname(_faces_dir()),
                            str(storage_path).replace("storage/", "", 1))
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as handle:
            return prepare_upload(handle.read(),
                                  original_filename=os.path.basename(full)
                                  ).embedding_normalized
    except Exception:                                             # noqa: BLE001
        return None


def _canonical(members, identities, images):
    """Pick the surviving UUID deterministically.

    Ordered by: most distinct photos (most evidence), then earliest created
    (the original record), then lowest UUID (a tiebreak that never depends on
    row order). Deterministic is the point — two runs must agree, or the script
    is not idempotent.
    """
    per_identity = defaultdict(set)
    for image in images:
        per_identity[image["identity_id"]].add(image["checksum"])

    def key(identity_id):
        info = identities.get(identity_id, {})
        return (-len(per_identity.get(identity_id, ())),
                info.get("created_at") or "",
                identity_id)

    return sorted(members, key=key)[0]


def build_plan(identities, embeddings, images, cohesion_threshold: float):
    """What WOULD change. Pure function of the loaded state — no I/O.

    EVERY GROUP IS ANCHORED TO ONE EXACT VECTOR. An earlier version linked
    identities transitively — A shares a vector with B, B shares a *different*
    vector with C, therefore {A,B,C} — and on this database that proposed
    folding thirteen identities together, including two whose vectors score
    -0.08 against each other. B was contaminated (it held two people's faces),
    and transitivity spread that contamination into a merge.

    So: identities are grouped only when they store the byte-identical vector,
    which is proof they were enrolled from the same photo. Anything an identity
    holds that does NOT match its group's anchor is a contaminated embedding and
    is removed rather than carried over.
    """
    import numpy as np

    by_vector = defaultdict(set)
    for row in embeddings:
        if row["vector"] is None or row["vector"].size == 0:
            continue
        by_vector[_signature(row["vector"])].add(row["identity_id"])
    shared_vectors = {sig: ids for sig, ids in by_vector.items() if len(ids) > 1}

    by_checksum = defaultdict(set)
    for image in images:
        by_checksum[image["checksum"]].add(image["identity_id"])
    shared_checksums = {csum: ids for csum, ids in by_checksum.items() if len(ids) > 1}

    plan = {
        "groups": [], "shared_vectors": shared_vectors,
        "shared_checksums": shared_checksums,
        "degenerate": _degenerate(embeddings),
        "contaminated": [], "reattributions": [], "manual_review": [],
        "_images": images,
    }

    images_by_identity = defaultdict(list)
    for image in images:
        images_by_identity[image["identity_id"]].append(image)
    embeddings_by_identity = defaultdict(list)
    for row in embeddings:
        embeddings_by_identity[row["identity_id"]].append(row)

    anchor_vector = {}
    for row in embeddings:
        if row["vector"] is None or row["vector"].size == 0:
            continue
        anchor_vector.setdefault(_signature(row["vector"]), row["vector"])

    # Deterministic order, and an identity is consumed by exactly one group:
    # one that holds two shared vectors would otherwise be consolidated twice.
    consumed = set()

    # Two anchor kinds, vectors first. A shared exact VECTOR is the strongest
    # evidence (the same photo produced both). A shared exact FILE is equally
    # conclusive about the photo but can survive with differing embeddings —
    # e.g. one identity's vector was later replaced — so those groups are
    # handled second, over whatever the vector pass did not already consume.
    anchors = [("vector", sig, shared_vectors[sig]) for sig in sorted(shared_vectors)]
    anchors += [("checksum", csum, shared_checksums[csum])
                for csum in sorted(shared_checksums)]

    first_vector_of = {}
    for row in embeddings:
        if row["vector"] is not None and row["vector"].size:
            first_vector_of.setdefault(row["identity_id"], row["vector"])

    for kind, key, candidate_ids in anchors:
        members = sorted(m for m in candidate_ids if m not in consumed)
        if len(members) < 2:
            continue
        canonical = _canonical(members, identities, images)
        if kind == "vector":
            anchor = anchor_vector[key]
        else:
            # A SHARED FILE IS NOT A SHARED PERSON. Two identities can hold the
            # same bytes because a file was mis-attached, and merging on that
            # alone would delete a real person's only face. So cohesion is
            # required first, and when the faces disagree the file — not the
            # identity — is what gets corrected.
            anchor = first_vector_of.get(canonical)
            if anchor is None:
                anchor = np.zeros(1, dtype=np.float32)

            disagree = [m for m in members
                        if first_vector_of.get(m) is not None
                        and anchor.size == first_vector_of[m].size
                        and float(np.dot(anchor, first_vector_of[m])) < cohesion_threshold]
            if disagree:
                contested = [img for img in images if img["checksum"] == key
                             and img["identity_id"] in members]
                file_face = _embed_stored_file(contested[0]["storage_path"]) if contested else None
                scored = []
                for member in members:
                    face = first_vector_of.get(member)
                    if file_face is not None and face is not None and face.size == file_face.size:
                        scored.append((float(np.dot(file_face, face)), member))
                scored.sort(reverse=True)

                # Clear winner: matches the file AND beats the runner-up by a
                # real margin. Anything less is a judgement call, and this
                # script does not make judgement calls.
                clear = (len(scored) >= 1 and scored[0][0] >= cohesion_threshold
                         and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.15))
                if clear:
                    keeper = scored[0][1]
                    plan["reattributions"].append({
                        "checksum": key, "keep": keeper,
                        "similarity": scored[0][0],
                        "drop_rows": [img["id"] for img in contested
                                      if img["identity_id"] != keeper],
                        "drop_from": [img["identity_id"] for img in contested
                                      if img["identity_id"] != keeper],
                    })
                else:
                    plan["manual_review"].append({
                        "checksum": key, "members": members,
                        "scores": scored,
                        "reason": "faces disagree and the file matches neither clearly",
                    })
                continue    # never merge on a contested file
        obsolete = [m for m in members if m != canonical]
        consumed.update(members)

        kept_checksums = {img["checksum"] for img in images_by_identity[canonical]}
        move, skip = [], []
        for identity_id in obsolete:
            for image in images_by_identity[identity_id]:
                if image["checksum"] in kept_checksums:
                    # Requirement 6: an exact duplicate file is skipped, not
                    # moved — the canonical identity already holds these bytes.
                    skip.append(image)
                else:
                    # Requirement 5: a DIFFERENT photo is preserved under the
                    # canonical UUID rather than deleted with its identity.
                    move.append(image)
                    kept_checksums.add(image["checksum"])

        seen_signatures = set()
        keep_embeddings, drop_embeddings, contaminated = [], [], []
        for identity_id in [canonical] + obsolete:
            for row in embeddings_by_identity[identity_id]:
                if row["vector"] is None or row["vector"].size == 0:
                    drop_embeddings.append(row)
                    continue
                row_signature = _signature(row["vector"])
                if row_signature in seen_signatures:
                    drop_embeddings.append(row)          # exact duplicate
                    continue
                cohesion = (float(np.dot(anchor, row["vector"]))
                            if anchor.size == row["vector"].size else 1.0)
                if cohesion < cohesion_threshold:
                    # Requirement 7: a face that is not this person. Carrying
                    # it over is exactly how one identity comes to match two
                    # different people at 1.0.
                    contaminated.append((row, cohesion))
                    drop_embeddings.append(row)
                    continue
                seen_signatures.add(row_signature)
                keep_embeddings.append(row)

        plan["contaminated"].extend(contaminated)
        plan["groups"].append({
            "canonical": canonical, "obsolete": obsolete, "anchor": key,
            "move_images": move, "skip_images": skip,
            "keep_embeddings": keep_embeddings,
            "drop_embeddings": drop_embeddings,
            "contaminated": contaminated,
        })

    return plan


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def print_plan(plan, identities, applied: bool):
    bar = "=" * 78
    print(bar)
    print("IDENTITY CONSOLIDATION — " + ("APPLIED" if applied else "DRY RUN (nothing changed)"))
    print(bar)

    def name(identity_id):
        info = identities.get(identity_id) or {}
        return f"{(info.get('display_name') or '?')[:26]:26} {identity_id[:8]}…"

    print(f"\nExact embeddings shared across identities : {len(plan['shared_vectors'])} group(s)")
    print(f"Exact image checksums shared across ids   : {len(plan['shared_checksums'])} group(s)")
    print(f"Degenerate embeddings (zero/non-finite)   : {len(plan['degenerate'])}")
    print(f"Contaminated embeddings (wrong person)    : {len(plan['contaminated'])}")

    print(f"Mis-attached files to correct             : {len(plan['reattributions'])}")
    print(f"Left for manual review                    : {len(plan['manual_review'])}")

    for item in plan["reattributions"]:
        print(f"\n--- contested file {item['checksum'][:12]}… ---")
        print(f"  the file's own face matches {name(item['keep'])} "
              f"(cosine {item['similarity']:+.4f})")
        print(f"  removing the association from {len(item['drop_rows'])} other identity(ies)"
              f" — NOT merging them")

    for item in plan["manual_review"]:
        print(f"\n--- contested file {item['checksum'][:12]}… — MANUAL REVIEW ---")
        print(f"  {item['reason']}")
        for score, member in item["scores"]:
            print(f"    {name(member)} cosine {score:+.4f}")

    if (not plan["groups"] and not plan["degenerate"]
            and not plan["reattributions"]):
        print("\nNothing to do — no shared vectors, no shared checksums, no bad rows.")
        print(bar)
        return

    for index, group in enumerate(plan["groups"], start=1):
        print(f"\n--- group {index}: {len(group['obsolete']) + 1} identities ---")
        print(f"  KEEP   {name(group['canonical'])}")
        for identity_id in group["obsolete"]:
            print(f"  REMOVE {name(identity_id)}")
        print(f"  photos moved to the kept identity : {len(group['move_images'])}")
        print(f"  exact duplicate files skipped     : {len(group['skip_images'])}")
        print(f"  embeddings kept                   : {len(group['keep_embeddings'])}")
        print(f"  embeddings removed as duplicates  : "
              f"{len(group['drop_embeddings']) - len(group['contaminated'])}")
        for row, cohesion in group["contaminated"]:
            print(f"  CONTAMINATED embedding id={row['id']} "
                  f"(cosine {cohesion:+.4f} to this group's face) — removed")

    for row, reason in plan["degenerate"]:
        print(f"  DEGENERATE embedding id={row['id']} identity={row['identity_id'][:8]}… ({reason})")

    print(bar)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

async def _system_actor(db):
    """The `system` principal, for audit rows nobody should attribute to a human."""
    from sqlalchemy import text
    return (await db.execute(text(
        "SELECT id FROM users WHERE username = 'system' LIMIT 1"))).scalar()


async def _audit(db, actor_id, action, identity_id=None, details=None):
    from sqlalchemy import text
    import json as _json

    if actor_id is None:
        return
    await db.execute(text(
        "INSERT INTO identity_audit_log (user_id, username, action_type, identity_id, "
        "                                action_details, success, created_at) "
        "VALUES (:u, 'system', :a, CAST(:i AS uuid), CAST(:d AS jsonb), true, now())"),
        {"u": actor_id, "a": action, "i": identity_id,
         "d": _json.dumps(details or {})})


async def apply_plan(db, plan, identities):
    """Execute the plan. One transaction; the caller commits."""
    from sqlalchemy import text

    actor_id = await _system_actor(db)
    images_by_checksum = defaultdict(list)
    for image in plan.get("_images", []):
        images_by_checksum[image["checksum"]].append(image)
    if actor_id is None:
        print("  ! no `system` principal found — run alembic upgrade head; "
              "continuing without audit rows")

    # Every table that references identities.id, discovered rather than
    # hard-coded. A wrong guess here is not a caught exception: in PostgreSQL a
    # failed statement aborts the whole transaction, so `try/except: pass`
    # around an optional UPDATE poisons everything after it — which is exactly
    # how the first attempt at this failed.
    referencing = [(row[0], row[1]) for row in (await db.execute(text(
        "SELECT tc.table_name, kcu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name "
        "JOIN information_schema.constraint_column_usage ccu "
        "  ON tc.constraint_name = ccu.constraint_name "
        "WHERE tc.constraint_type = 'FOREIGN KEY' "
        "  AND ccu.table_name = 'identities' AND ccu.column_name = 'id'"))).all()]

    faces_root = _faces_dir()
    # FILE OPERATIONS ARE DEFERRED. An earlier version moved and deleted files
    # inside this loop; when a later statement raised, the transaction rolled
    # back but the files were already gone, leaving rows pointing at nothing.
    # Ops are collected here and executed by the caller AFTER the commit
    # succeeds, and `_repair_orphans` reconciles anything a crash interrupts.
    file_ops = []

    for group in plan["groups"]:
        canonical = group["canonical"]
        canonical_dir = _assert_inside_faces(os.path.join(faces_root, canonical))
        os.makedirs(canonical_dir, exist_ok=True)

        # --- move the photos worth keeping ---------------------------------
        for image in group["move_images"]:
            source = _assert_inside_faces(
                os.path.join(os.path.dirname(faces_root),
                             image["storage_path"].replace("storage/", "", 1))
                if image["storage_path"].startswith("storage/")
                else os.path.join(faces_root, image["identity_id"],
                                  os.path.basename(image["storage_path"])))
            filename = os.path.basename(image["storage_path"])
            target = _assert_inside_faces(os.path.join(canonical_dir, filename))
            # Never clobber: a name collision means two different photos
            # happened to be image_001.jpg under two identities.
            stem, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(target):
                counter += 1
                target = _assert_inside_faces(
                    os.path.join(canonical_dir, f"{stem}_{counter}{ext}"))
            file_ops.append(("move", source, target))
            await db.execute(text(
                "UPDATE identity_images SET identity_id = CAST(:c AS uuid), "
                "       storage_path = :p, is_primary = false WHERE id = :i"),
                {"c": canonical,
                 "p": "storage/" + os.path.relpath(target,
                                                   os.path.dirname(faces_root)).replace("\\", "/"),
                 "i": image["id"]})

        # --- drop the exact-duplicate files and their rows -----------------
        for image in group["skip_images"]:
            candidate = os.path.join(faces_root, image["identity_id"],
                                     os.path.basename(image["storage_path"]))
            file_ops.append(("delete", _assert_inside_faces(candidate), None))
            await db.execute(text("DELETE FROM identity_images WHERE id = :i"),
                             {"i": image["id"]})

        # --- embeddings: keep one per distinct vector on the canonical -----
        keep_ids = [row["id"] for row in group["keep_embeddings"]]
        drop_ids = [row["id"] for row in group["drop_embeddings"]]
        if keep_ids:
            await db.execute(text(
                "UPDATE identity_embeddings SET identity_id = CAST(:c AS uuid), "
                "       vector_index_sync_state = 'pending' "
                "WHERE id = ANY(:ids)"), {"c": canonical, "ids": keep_ids})
        if drop_ids:
            await db.execute(text("DELETE FROM identity_embeddings WHERE id = ANY(:ids)"),
                             {"ids": drop_ids})

        # --- the obsolete identities go, with their now-empty folders ------
        for identity_id in group["obsolete"]:
            await db.execute(text(
                "UPDATE identity_appearances SET identity_id = CAST(:c AS uuid) "
                "WHERE identity_id = CAST(:o AS uuid)"),
                {"c": canonical, "o": identity_id})
            # identity_audit_log FKs identities in BOTH directions. Repointed to
            # the survivor rather than deleted: consolidating records must not
            # erase the history of what was done to them.
            await db.execute(text(
                "UPDATE identity_audit_log SET identity_id = CAST(:c AS uuid) "
                "WHERE identity_id = CAST(:o AS uuid)"),
                {"c": canonical, "o": identity_id})
            await db.execute(text(
                "UPDATE identity_audit_log SET related_identity_id = CAST(:c AS uuid) "
                "WHERE related_identity_id = CAST(:o AS uuid)"),
                {"c": canonical, "o": identity_id})
            for table, column in referencing:
                if (table, column) in (("identity_images", "identity_id"),
                                       ("identity_embeddings", "identity_id"),
                                       ("identity_appearances", "identity_id"),
                                       ("identity_audit_log", "identity_id"),
                                       ("identity_audit_log", "related_identity_id")):
                    continue      # handled explicitly above
                await db.execute(text(
                    f"UPDATE {table} SET {column} = CAST(:c AS uuid) "
                    f"WHERE {column} = CAST(:o AS uuid)"),
                    {"c": canonical, "o": identity_id})
            await db.execute(text("DELETE FROM identity_images WHERE identity_id = CAST(:o AS uuid)"),
                             {"o": identity_id})
            await db.execute(text("DELETE FROM identity_embeddings WHERE identity_id = CAST(:o AS uuid)"),
                             {"o": identity_id})
            await db.execute(text("DELETE FROM identities WHERE id = CAST(:o AS uuid)"),
                             {"o": identity_id})
            folder = os.path.join(faces_root, identity_id)
            file_ops.append(("rmtree", _assert_inside_faces(folder), None))

        await _audit(db, actor_id, "identity_consolidated", canonical, {
            "removed_identities": group["obsolete"],
            "photos_moved": len(group["move_images"]),
            "duplicate_files_skipped": len(group["skip_images"]),
            "embeddings_removed": len(drop_ids),
        })

    # --- contested files: correct the association, never merge -------------
    for item in plan["reattributions"]:
        if item["drop_rows"]:
            await db.execute(text("DELETE FROM identity_images WHERE id = ANY(:ids)"),
                             {"ids": item["drop_rows"]})
            for identity_id in item["drop_from"]:
                folder = os.path.join(faces_root, identity_id)
                for image in images_by_checksum.get(item["checksum"], []):
                    if image["identity_id"] == identity_id:
                        file_ops.append(
                            ("delete",
                             _assert_inside_faces(os.path.join(
                                 folder, os.path.basename(image["storage_path"]))),
                             None))
            await _audit(db, actor_id, "misattached_file_removed", item["keep"], {
                "checksum_prefix": item["checksum"][:12],
                "kept_by_face_similarity": round(item["similarity"], 4),
                "removed_from": item["drop_from"],
            })

    # --- degenerate rows go regardless of grouping -------------------------
    bad_ids = [row["id"] for row, _reason in plan["degenerate"]]
    if bad_ids:
        await db.execute(text("DELETE FROM identity_embeddings WHERE id = ANY(:ids)"),
                         {"ids": bad_ids})
        await _audit(db, actor_id, "degenerate_embeddings_removed", None,
                     {"count": len(bad_ids)})

    # --- repair primary image + best_snapshot_path -------------------------
    repaired = await _repair_primaries(db)
    await _audit(db, actor_id, "identity_snapshots_repaired", None,
                 {"identities": repaired})

    return file_ops, repaired


def execute_file_ops(file_ops):
    """Run the deferred moves/deletes. Only ever called AFTER a successful commit."""
    moved = removed = 0
    for kind, source, target in file_ops:
        try:
            if kind == "move" and os.path.isfile(source):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(source, target)
                moved += 1
            elif kind == "delete" and os.path.isfile(source):
                os.remove(source)
                removed += 1
            elif kind == "rmtree" and os.path.isdir(source):
                for entry in os.listdir(source):
                    os.remove(os.path.join(source, entry))
                os.rmdir(source)
        except OSError as exc:
            print(f"  ! file op {kind} failed on {os.path.basename(source)}: {exc}")
    return moved, removed


async def repair_orphans(db):
    """Reconcile rows against the filesystem, and drop empty folders.

    This is what makes the whole run restart-repairable: if the process dies
    between the commit and the file moves, or a previous run left rows pointing
    at files it had already deleted, this brings the two back into agreement
    without needing to know how they diverged.
    """
    from sqlalchemy import text

    faces_root = _faces_dir()
    storage_root = os.path.dirname(faces_root)

    dropped = 0
    for image_id, storage_path in (await db.execute(text(
            "SELECT id, storage_path FROM identity_images"))).all():
        full = os.path.join(storage_root, str(storage_path).replace("storage/", "", 1))
        if not os.path.isfile(full):
            await db.execute(text("DELETE FROM identity_images WHERE id = :i"),
                             {"i": image_id})
            dropped += 1

    await db.execute(text(
        "UPDATE identities SET best_snapshot_path = NULL WHERE best_snapshot_path "
        "IS NOT NULL AND NOT EXISTS (SELECT 1 FROM identity_images m "
        "WHERE m.identity_id = identities.id "
        "  AND m.storage_path = identities.best_snapshot_path)"))

    removed_dirs = 0
    if os.path.isdir(faces_root):
        for entry in sorted(os.listdir(faces_root)):
            folder = os.path.join(faces_root, entry)
            if entry.startswith(".") or not os.path.isdir(folder):
                continue
            if not os.listdir(folder):
                os.rmdir(_assert_inside_faces(folder))
                removed_dirs += 1
    return dropped, removed_dirs


async def _repair_primaries(db):
    """Exactly one primary per identity, and a snapshot that points at it.

    Runs for EVERY identity, not just consolidated ones: `best_snapshot_path`
    was being overwritten on any positive similarity, so identities untouched
    by consolidation can still be displaying a file they do not own.
    """
    from sqlalchemy import text

    rows = (await db.execute(text(
        "SELECT i.id::text, "
        "       (SELECT count(*) FROM identity_images m WHERE m.identity_id = i.id) "
        "FROM identities i"))).all()

    repaired = 0
    for identity_id, image_count in rows:
        if not image_count:
            await db.execute(text(
                "UPDATE identities SET best_snapshot_path = NULL "
                "WHERE id = CAST(:i AS uuid) AND best_snapshot_path IS NOT NULL"),
                {"i": identity_id})
            continue

        chosen = (await db.execute(text(
            "SELECT id, storage_path FROM identity_images "
            "WHERE identity_id = CAST(:i AS uuid) "
            "ORDER BY quality_score DESC NULLS LAST, created_at ASC LIMIT 1"),
            {"i": identity_id})).first()
        if not chosen:
            continue
        await db.execute(text(
            "UPDATE identity_images SET is_primary = false "
            "WHERE identity_id = CAST(:i AS uuid) AND id <> :k"),
            {"i": identity_id, "k": chosen[0]})
        await db.execute(text(
            "UPDATE identity_images SET is_primary = true WHERE id = :k"),
            {"k": chosen[0]})
        await db.execute(text(
            "UPDATE identities SET best_snapshot_path = :p "
            "WHERE id = CAST(:i AS uuid) AND "
            "      (best_snapshot_path IS DISTINCT FROM :p)"),
            {"p": chosen[1], "i": identity_id})
        repaired += 1
    return repaired


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

async def verify(db):
    """The completion gates, checked against live state. Returns problems."""
    from sqlalchemy import text
    import numpy as np

    problems = []
    identities, embeddings, images = await _load_state(db)

    active = {i for i, info in identities.items()
              if info["status"] in ("ACTIVE", "PROMOTED")}

    by_vector = defaultdict(set)
    for row in embeddings:
        if row["vector"] is None or row["vector"].size == 0:
            continue
        if row["identity_id"] in active:
            by_vector[_signature(row["vector"])].add(row["identity_id"])
    shared = {s: ids for s, ids in by_vector.items() if len(ids) > 1}
    if shared:
        problems.append(f"{len(shared)} exact embedding(s) shared across active identities")

    by_checksum = defaultdict(set)
    for image in images:
        if image["identity_id"] in active:
            by_checksum[image["checksum"]].add(image["identity_id"])
    shared_files = {c: ids for c, ids in by_checksum.items() if len(ids) > 1}
    if shared_files:
        problems.append(f"{len(shared_files)} exact image checksum(s) shared across active identities")

    degenerate = _degenerate(embeddings)
    if degenerate:
        problems.append(f"{len(degenerate)} degenerate embedding(s) remain")

    multi_primary = (await db.execute(text(
        "SELECT count(*) FROM (SELECT identity_id FROM identity_images "
        "WHERE is_primary GROUP BY identity_id HAVING count(*) > 1) t"))).scalar()
    if multi_primary:
        problems.append(f"{multi_primary} identity(ies) have more than one primary image")

    orphan_snapshot = (await db.execute(text(
        "SELECT count(*) FROM identities i WHERE i.best_snapshot_path IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM identity_images m "
        "                WHERE m.identity_id = i.id "
        "                  AND m.storage_path = i.best_snapshot_path)"))).scalar()
    if orphan_snapshot:
        problems.append(f"{orphan_snapshot} identity(ies) display a snapshot they do not own")

    missing_files = 0
    faces_root = _faces_dir()
    for image in images:
        path = os.path.join(os.path.dirname(faces_root),
                            image["storage_path"].replace("storage/", "", 1))
        if not os.path.isfile(path):
            missing_files += 1
    if missing_files:
        problems.append(f"{missing_files} image row(s) point at a missing file")

    stale = (await db.execute(text(
        "SELECT count(*) FROM identity_embeddings WHERE vector_index_sync_state "
        "NOT IN ('synced', 'pending', 'failed')"))).scalar()
    if stale:
        problems.append(f"{stale} embedding(s) carry an unknown sync state")

    return problems


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

async def _reconcile_vector_index(db):
    """pgvector needs no rebuild (rows ARE the index); FAISS does."""
    from config import settings
    if str(settings.VECTOR_BACKEND).lower() == "pgvector":
        from sqlalchemy import text
        await db.execute(text(
            "UPDATE identity_embeddings SET vector_index_sync_state = 'synced' "
            "WHERE vector_index_sync_state = 'pending'"))
        return "pgvector: rows are authoritative; pending marked synced"
    try:
        from backend.core.vector_index.manager import vector_index_manager
        report = await vector_index_manager.rebuild_from_db(db)
        return f"faiss: rebuilt {report}"
    except Exception as exc:                                      # noqa: BLE001
        return f"faiss: rebuild deferred to reconciliation ({type(exc).__name__})"


async def main_async(args) -> int:
    from db_connection import db_manager

    await db_manager.init_db()

    if args.verify:
        async with db_manager.get_session() as db:
            problems = await verify(db)
        bar = "=" * 78
        print(bar)
        if problems:
            print(f"VERIFY FAILED — {len(problems)} problem(s)")
            for problem in problems:
                print(f"  ✗ {problem}")
            print(bar)
            return 1
        print("VERIFY PASSED")
        print(bar)
        for line in (
            "no exact embedding shared across active identities",
            "no exact image checksum shared across active identities",
            "no zero-norm or non-finite embedding",
            "exactly one primary image per identity",
            "every displayed snapshot belongs to its identity",
            "every image row points at a file that exists",
        ):
            print(f"  ✓ {line}")
        print(bar)
        return 0

    async with db_manager.get_session() as db:
        identities, embeddings, images = await _load_state(db)
        from config import settings
        # The same bar recognition uses: anything below it is, by the
        # operator's own configuration, not this person.
        cohesion_threshold = float(settings.SIMILARITY_THRESHOLD)
        plan = build_plan(identities, embeddings, images, cohesion_threshold)

    if not args.apply:
        print_plan(plan, identities, applied=False)
        print("\nThis was a DRY RUN. Re-run with --apply --yes-i-understand to change data.")
        return 0

    print_plan(plan, identities, applied=False)
    if not args.yes_i_understand:
        print("\nREFUSING: --apply also requires --yes-i-understand.")
        return 2
    if not args.no_prompt:
        typed = input(f'\nType exactly "{CONFIRM_PHRASE}" to proceed: ').strip()
        if typed != CONFIRM_PHRASE:
            print("Phrase did not match. Nothing was changed.")
            return 2

    async with db_manager.get_session() as db:
        file_ops, repaired = await apply_plan(db, plan, identities)
        note = await _reconcile_vector_index(db)
        await db.commit()

    # Only now that the database is durable do we touch the filesystem.
    moved, removed = execute_file_ops(file_ops)

    # Reconcile whatever the file operations changed, and sweep empty folders.
    # Also repairs anything a previous interrupted run left behind.
    async with db_manager.get_session() as db:
        dropped, removed_dirs = await repair_orphans(db)
        await _repair_primaries(db)
        await db.commit()

    print(f"\n  files moved to a kept identity : {moved}")
    print(f"  duplicate files removed        : {removed}")
    print(f"  orphaned image rows dropped    : {dropped}")
    print(f"  empty identity folders removed : {removed_dirs}")
    print(f"  identities with snapshot repaired: {repaired}")
    print(f"  vector index                   : {note}")

    async with db_manager.get_session() as db:
        problems = await verify(db)
    if problems:
        print(f"\nPOST-APPLY VERIFY FOUND {len(problems)} PROBLEM(S):")
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1
    print("\nPost-apply verify passed.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="actually change data (default is a dry run)")
    parser.add_argument("--yes-i-understand", action="store_true",
                        help="required alongside --apply")
    parser.add_argument("--no-prompt", action="store_true",
                        help="skip the typed confirmation (for non-interactive runs)")
    parser.add_argument("--verify", action="store_true",
                        help="check the completion gates and exit")
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
