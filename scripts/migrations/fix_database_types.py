"""
Fix Database Type Conflicts
===========================
Script to clean up PostgreSQL type conflicts that prevent table creation.
Run this if you get "duplicate key value violates unique constraint pg_type_typname_nsp_index" errors.
"""
import os
import sys
import asyncio

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import create_engine, text
from config import settings

def fix_database_types():
    """Fix PostgreSQL type conflicts"""
    # Get database URL and convert to sync
    database_url = settings.DATABASE_URL
    
    # If running outside Docker, replace container name with localhost
    if "postgres:" in database_url and "@postgres:" in database_url:
        print("⚠️  Detected Docker container hostname 'postgres'")
        print("   Attempting to use 'localhost' instead...")
        database_url = database_url.replace("@postgres:", "@localhost:")
    
    # Convert async URL to sync for migration
    if "asyncpg" in database_url:
        database_url = database_url.replace("asyncpg", "psycopg2")
    elif "postgresql+asyncpg" in database_url:
        database_url = database_url.replace("postgresql+asyncpg", "postgresql+psycopg2")
    
    print(f"🔗 Connecting to database...")
    print(f"   URL: {database_url.split('@')[1] if '@' in database_url else 'hidden'}")
    
    try:
        # Create sync engine
        engine = create_engine(database_url, echo=False)
        
        with engine.connect() as conn:
            trans = conn.begin()
            
            try:
                print("\n📋 Checking for type conflicts...")
                
                # Check for orphaned types
                result = conn.execute(text("""
                    SELECT typname, typnamespace::regnamespace as schema_name
                    FROM pg_type
                    WHERE typname IN ('pipelines', 'detections', 'faces', 'users', 'user_pipeline_access', 'chatbot_audit_log')
                    AND typnamespace = 2200
                """))
                
                orphaned_types = result.fetchall()
                
                if orphaned_types:
                    print(f"⚠️  Found {len(orphaned_types)} potentially orphaned types:")
                    for typname, schema_name in orphaned_types:
                        print(f"   - {typname} in {schema_name}")
                    
                    print("\n⚠️  WARNING: Dropping types can cause data loss!")
                    print("   Only proceed if you're sure the tables don't exist or you have backups.")
                    response = input("\n   Do you want to drop these types? (yes/no): ")
                    
                    if response.lower() == 'yes':
                        for typname, schema_name in orphaned_types:
                            try:
                                print(f"   Dropping type {typname}...")
                                conn.execute(text(f"DROP TYPE IF EXISTS {typname} CASCADE"))
                                print(f"   ✅ Dropped {typname}")
                            except Exception as e:
                                print(f"   ❌ Failed to drop {typname}: {e}")
                    else:
                        print("   Skipping type cleanup")
                else:
                    print("✅ No orphaned types found")
                
                # Check if tables exist
                print("\n📋 Checking existing tables...")
                result = conn.execute(text("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                """))
                
                existing_tables = [row[0] for row in result]
                
                if existing_tables:
                    print(f"✅ Found {len(existing_tables)} existing tables:")
                    for table in existing_tables:
                        print(f"   - {table}")
                else:
                    print("ℹ️  No tables found (database is empty)")
                
                trans.commit()
                
                print("\n✅ Database check completed!")
                print("\n💡 If you still get errors, try:")
                print("   1. Drop and recreate the database")
                print("   2. Or manually drop the conflicting types:")
                print("      DROP TYPE IF EXISTS pipelines CASCADE;")
                
            except Exception as e:
                trans.rollback()
                raise e
                
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nTroubleshooting:")
        print("1. Make sure PostgreSQL is running")
        print("2. Check DATABASE_URL in .env file or config")
        print("3. Ensure psycopg2-binary is installed: pip install psycopg2-binary")
        import traceback
        traceback.print_exc()
        return False
    
    return True

if __name__ == "__main__":
    success = fix_database_types()
    sys.exit(0 if success else 1)

