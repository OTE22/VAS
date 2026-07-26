"""
Migration script to add blocked_reason and blocked_at columns to users table
Run this script to add the new columns for user blocking functionality.
"""
import os
import sys
import asyncio

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import create_engine, text, inspect
from config import settings

def run_migration():
    """Add blocked_reason and blocked_at columns to users table"""
    # Get database URL and convert to sync connection
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
            # Start transaction
            trans = conn.begin()
            
            try:
                # Check if users table exists
                inspector = inspect(engine)
                if 'users' not in inspector.get_table_names():
                    print("❌ Error: 'users' table does not exist!")
                    print("   Please run the initial migration first (run_migration.py)")
                    trans.rollback()
                    return False
                
                # Get existing columns
                existing_columns = [col['name'] for col in inspector.get_columns('users')]
                print(f"📋 Existing columns in 'users' table: {', '.join(existing_columns)}")
                
                columns_to_add = []
                
                # Check if blocked_reason column exists
                if 'blocked_reason' not in existing_columns:
                    columns_to_add.append(('blocked_reason', 'TEXT'))
                    print("  ➕ Will add: blocked_reason (TEXT)")
                else:
                    print("  ✅ blocked_reason already exists")
                
                # Check if blocked_at column exists
                if 'blocked_at' not in existing_columns:
                    columns_to_add.append(('blocked_at', 'TIMESTAMP'))
                    print("  ➕ Will add: blocked_at (TIMESTAMP)")
                else:
                    print("  ✅ blocked_at already exists")
                
                if not columns_to_add:
                    print("\n✅ All columns already exist! No migration needed.")
                    trans.rollback()  # No changes needed
                    return True
                
                # Add columns
                print(f"\n📦 Adding {len(columns_to_add)} column(s)...")
                for column_name, column_type in columns_to_add:
                    print(f"   Adding {column_name} ({column_type})...")
                    conn.execute(text(f"""
                        ALTER TABLE users 
                        ADD COLUMN {column_name} {column_type} NULL
                    """))
                    print(f"   ✅ Added {column_name}")
                
                # Commit transaction
                trans.commit()
                
                print("\n✅ Migration completed successfully!")
                print("   Added columns:")
                for column_name, _ in columns_to_add:
                    print(f"   - {column_name}")
                
                return True
                
            except Exception as e:
                trans.rollback()
                raise e
                
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        print("\nTroubleshooting:")
        print("1. Make sure PostgreSQL is running")
        print("2. Check DATABASE_URL in .env file or config")
        print("3. Ensure psycopg2-binary is installed: pip install psycopg2-binary")
        print("4. Ensure the 'users' table exists (run run_migration.py first if needed)")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)

