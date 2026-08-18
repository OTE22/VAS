#!/usr/bin/env python3
"""
Backfill identity_images from existing best_snapshot_path values
================================================================
OPTIONAL and idempotent. The multi-image migration (d0e1f2a3b4c5) is pure
DDL; this script is the separate, reviewable step that gives already-enrolled
people an IdentityImage row for the photo they already have.

What it does, per active KNOWN identity that has a best_snapshot_path:
  * resolves the path under STORAGE_DIR (refusing anything that escapes it)
  * skips when the file is missing — reported, never invented
  * computes the SHA-256 of the real bytes on disk
  * inserts ONE IdentityImage marked is_primary=true
  * links existing embeddings to it ONLY when the relationship is unambiguous
    (exactly one embedding for that identity with image_id IS NULL and
    pipeline_id='uploaded'); anything else is reported as unresolved

What it deliberately does NOT do:
  * move, rename or delete any file — existing display-name folders stay put
  * guess which of several embeddings came from which photo
  * create identities

Usage:
    python scripts/backfill_identity_images.py --dry-run     # report only
    python scripts/backfill_identity_images.py               # apply
"""

import argparse
import asyncio
import hashlib
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, func  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("backfill_identity_images")


def _resolve(storage_path: str, storage_dir: str):
    """Absolute path for a stored relative path, or None if it escapes."""
    if not storage_path:
        return None
    candidate = storage_path
    if candidate.startswith("storage/"):
        candidate = candidate[len("storage/"):]
    absolute = os.path.abspath(os.path.join(storage_dir, candidate))
    if os.path.commonpath([os.path.abspath(storage_dir), absolute]) != os.path.abspath(storage_dir):
        return None
    return absolute


async def run(dry_run: bool) -> int:
    from config import settings
    from db_connection import db_manager
    from db_models import (Identity, IdentityEmbedding, IdentityImage,
                           IdentityStatus, IdentityType)

    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()

    storage_dir = settings.STORAGE_DIR
    created = linked = 0
    unresolved = []

    async with db_manager.get_session() as db:
        identities = (await db.execute(
            select(Identity).where(
                Identity.type == IdentityType.KNOWN,
                Identity.status == IdentityStatus.ACTIVE,
                Identity.best_snapshot_path.isnot(None),
            ))).scalars().all()

        logger.info("candidate identities with a best_snapshot_path: %d", len(identities))

        for identity in identities:
            existing = (await db.execute(
                select(func.count(IdentityImage.id))
                .where(IdentityImage.identity_id == identity.id))).scalar() or 0
            if existing:
                continue  # already backfilled or already enrolled the new way

            absolute = _resolve(identity.best_snapshot_path, storage_dir)
            if absolute is None:
                unresolved.append((identity.id, "path escapes storage dir"))
                continue
            if not os.path.isfile(absolute):
                unresolved.append((identity.id, "file missing on disk"))
                continue

            with open(absolute, "rb") as handle:
                payload = handle.read()
            checksum = hashlib.sha256(payload).hexdigest()

            width = height = None
            try:
                import cv2
                import numpy as np
                decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
                if decoded is not None:
                    height, width = decoded.shape[:2]
            except Exception:
                pass

            if dry_run:
                logger.info("[dry-run] would create image for %s (%s)",
                            identity.id, os.path.basename(absolute))
                created += 1
                continue

            image = IdentityImage(
                identity_id=identity.id,
                storage_path=identity.best_snapshot_path,
                original_filename=os.path.basename(absolute),
                file_checksum=checksum,
                content_type=None,
                file_size=len(payload),
                width=width,
                height=height,
                is_primary=True,
                processing_status="completed",
            )
            db.add(image)
            await db.flush()
            created += 1

            # Link ONLY when there is exactly one unambiguous candidate.
            candidates = (await db.execute(
                select(IdentityEmbedding).where(
                    IdentityEmbedding.identity_id == identity.id,
                    IdentityEmbedding.image_id.is_(None),
                    IdentityEmbedding.pipeline_id.is_(None),   # enrollment rows carry no camera
                ))).scalars().all()
            if len(candidates) == 1:
                candidates[0].image_id = image.id
                linked += 1
            elif len(candidates) > 1:
                unresolved.append(
                    (identity.id,
                     f"{len(candidates)} upload embeddings — cannot attribute to one photo"))

        if not dry_run:
            await db.commit()

    logger.info("images created: %d", created)
    logger.info("embeddings linked: %d", linked)
    logger.info("unresolved (reported, not guessed): %d", len(unresolved))
    for identity_id, reason in unresolved:
        logger.info("  %s — %s", identity_id, reason)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()
    return asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
