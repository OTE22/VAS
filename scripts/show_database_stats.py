#!/usr/bin/env python3
"""Quick script to show database statistics"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db_connection import db_manager
from sqlalchemy import select, func
from db_models import Detection, Face, Identity, IdentityEmbedding, IdentityAppearance

async def show_stats():
    await db_manager.init_db()
    async with db_manager.get_session() as db:
        detections = (await db.execute(select(func.count(Detection.id)))).scalar_one()
        faces = (await db.execute(select(func.count(Face.id)))).scalar_one()
        identities = (await db.execute(select(func.count(Identity.id)))).scalar_one()
        embeddings = (await db.execute(select(func.count(IdentityEmbedding.id)))).scalar_one()
        appearances = (await db.execute(select(func.count(IdentityAppearance.id)))).scalar_one()
        
        print("=" * 60)
        print("📊 Database Statistics")
        print("=" * 60)
        print(f"   Detections:     {detections:,}")
        print(f"   Faces:          {faces:,}")
        print(f"   Identities:     {identities:,}")
        print(f"   Embeddings:     {embeddings:,}")
        print(f"   Appearances:    {appearances:,}")
        print("=" * 60)
        print(f"\n💡 Each face detection creates:")
        print(f"   ✅ 1 Detection record (per image/frame)")
        print(f"   ✅ 1 Face record (per face)")
        print(f"   ✅ 1 Identity record (per unique person, reused)")
        print(f"   ✅ 1 IdentityEmbedding record (per face, with pgvector)")
        print(f"   ✅ 1 IdentityAppearance record (per detection)")

if __name__ == "__main__":
    try:
        asyncio.run(show_stats())
    finally:
        asyncio.run(db_manager.close_db())

