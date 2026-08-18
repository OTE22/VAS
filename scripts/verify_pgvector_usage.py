#!/usr/bin/env python3
"""
Verify pgvector Usage
=====================
Check that pgvector is properly configured and used throughout the system.
"""

import os
import sys
import logging
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def check_config():
    """Check configuration for pgvector settings."""
    logger.info("=" * 80)
    logger.info("🔍 Verifying pgvector Configuration")
    logger.info("=" * 80)
    
    try:
        from config import settings
        
        vector_backend = settings.VECTOR_BACKEND.lower()
        logger.info(f"📊 VECTOR_BACKEND: {vector_backend}")
        
        if vector_backend == 'pgvector':
            logger.info("✅ pgvector is configured as the vector backend")
            
            # Check pgvector settings
            index_type = settings.PGVECTOR_INDEX_TYPE
            ef_search = settings.PGVECTOR_HNSW_EF_SEARCH
            ef_construction = settings.PGVECTOR_HNSW_EF_CONSTRUCTION
            
            logger.info(f"   • Index type: {index_type}")
            logger.info(f"   • HNSW ef_search: {ef_search}")
            logger.info(f"   • HNSW ef_construction: {ef_construction}")
        else:
            logger.warning(f"⚠️  VECTOR_BACKEND is '{vector_backend}', not 'pgvector'")
            logger.warning("   Set VECTOR_BACKEND=pgvector to use pgvector")
            return False
        
        return True
    except Exception as e:
        logger.error(f"❌ Error checking config: {e}")
        return False


def check_imports():
    """Check if pgvector modules are available."""
    logger.info("")
    logger.info("=" * 80)
    logger.info("🔍 Checking pgvector Module Availability")
    logger.info("=" * 80)
    
    try:
        from backend.core.identity_index_pgvector import IdentityIndexPgVector, identity_index_pgvector
        logger.info("✅ pgvector module imported successfully")
        
        if identity_index_pgvector:
            logger.info("✅ Global identity_index_pgvector instance exists")
        else:
            logger.warning("⚠️  Global identity_index_pgvector instance is None")
        
        return True
    except ImportError as e:
        logger.error(f"❌ Failed to import pgvector module: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Error checking imports: {e}")
        return False


def check_identity_service():
    """Check IdentityService initialization."""
    logger.info("")
    logger.info("=" * 80)
    logger.info("🔍 Checking IdentityService Configuration")
    logger.info("=" * 80)
    
    try:
        from backend.core.identity_service import IdentityService, USE_PGVECTOR, VECTOR_BACKEND
        
        logger.info(f"📊 USE_PGVECTOR: {USE_PGVECTOR}")
        logger.info(f"📊 VECTOR_BACKEND: {VECTOR_BACKEND}")
        
        if USE_PGVECTOR:
            logger.info("✅ IdentityService will use pgvector when initialized")
        else:
            logger.warning("⚠️  IdentityService will use FAISS (not pgvector)")
            logger.warning("   Check VECTOR_BACKEND setting")
            return False
        
        return True
    except Exception as e:
        logger.error(f"❌ Error checking IdentityService: {e}")
        return False


def check_code_paths():
    """Check critical code paths for pgvector usage."""
    logger.info("")
    logger.info("=" * 80)
    logger.info("🔍 Checking Critical Code Paths")
    logger.info("=" * 80)
    
    issues = []
    
    # Check identity_service.py
    try:
        with open('backend/core/identity_service.py', 'r') as f:
            content = f.read()
            
            # Check find_or_create_identity
            if 'if self.use_pgvector and self.pgvector_index:' in content:
                logger.info("✅ find_or_create_identity() checks for pgvector")
            else:
                issues.append("❌ find_or_create_identity() may not check for pgvector")
            
            # Check save_embedding
            if 'if self.use_pgvector and self.pgvector_index:' in content or 'pgvector_index.add_embedding' in content:
                logger.info("✅ save_embedding() uses pgvector when enabled")
            else:
                issues.append("⚠️  save_embedding() may not use pgvector")
            
    except Exception as e:
        logger.error(f"❌ Error checking code paths: {e}")
        return False
    
    if issues:
        logger.warning("")
        logger.warning("⚠️  Potential Issues Found:")
        for issue in issues:
            logger.warning(f"   {issue}")
        return False
    
    logger.info("✅ All critical code paths check for pgvector")
    return True


def main():
    """Run all checks."""
    logger.info("")
    logger.info("=" * 80)
    logger.info("🔍 pgvector Usage Verification")
    logger.info("=" * 80)
    logger.info("")
    
    results = {
        'config': check_config(),
        'imports': check_imports(),
        'identity_service': check_identity_service(),
        'code_paths': check_code_paths()
    }
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("📊 Verification Summary")
    logger.info("=" * 80)
    
    all_passed = all(results.values())
    
    for check, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        logger.info(f"   {check}: {status}")
    
    logger.info("")
    if all_passed:
        logger.info("✅ All checks passed! pgvector is properly configured.")
    else:
        logger.warning("⚠️  Some checks failed. Review the output above.")
    
    logger.info("=" * 80)
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

