"""
Migration script to add identity_id and label_state columns to faces table
This fixes the missing column error: column faces.identity_id does not exist
"""
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import create_engine, text
from config import settings

def run_migration():
    """Add identity_id and label_state columns to faces table if they don't exist"""
    # Get database URL and convert to sync connection
    database_url = settings.DATABASE_URL
    
    # Detect if running inside Docker container
    is_docker = os.path.exists("/.dockerenv")
    
    # If running outside Docker and URL contains 'postgres' hostname, replace with localhost
    if not is_docker and "postgres:" in database_url and "@postgres:" in database_url:
        print("⚠️  Detected Docker container hostname 'postgres'")
        print("   Running outside Docker, using 'localhost' instead...")
        database_url = database_url.replace("@postgres:", "@localhost:")
    elif is_docker:
        print("🐳 Running inside Docker container, using 'postgres' hostname")
    
    # Convert async URL to sync for migration
    if "asyncpg" in database_url:
        database_url = database_url.replace("asyncpg", "psycopg2")
    elif "postgresql+asyncpg" in database_url:
        database_url = database_url.replace("postgresql+asyncpg", "postgresql+psycopg2")
    
    print(f"🔗 Connecting to database...")
    print(f"   URL: {database_url.split('@')[1] if '@' in database_url else 'hidden'}")
    
    try:
        # Create sync engine with autocommit for DDL operations
        engine = create_engine(database_url, echo=False)
        
        # First, check existing columns and table structure
        with engine.connect() as conn:
            # Check which columns already exist
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_schema = 'public' 
                AND table_name = 'faces' 
                AND column_name IN ('identity_id', 'label_state')
            """))
            
            existing_columns = {row[0] for row in result}
            
            identity_id_exists = 'identity_id' in existing_columns
            label_state_exists = 'label_state' in existing_columns
            
            if identity_id_exists and label_state_exists:
                print("✅ Both columns already exist in 'faces' table!")
                print("   - identity_id: ✅")
                print("   - label_state: ✅")
                print("   Skipping migration.")
                return
            
            columns_to_add = []
            if not identity_id_exists:
                columns_to_add.append('identity_id')
            if not label_state_exists:
                columns_to_add.append('label_state')
            
            print(f"📋 Columns to add: {', '.join(columns_to_add)}")
            
            # Check if identities table exists (required for foreign key)
            result = conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'identities'
            """))
            
            identities_table_exists = result.fetchone() is not None
            
            if not identities_table_exists:
                print("⚠️  Warning: 'identities' table does not exist!")
                print("   The identity_id foreign key will be created without constraint.")
                print("   You may need to create the identities table first.")
            
            # Check if faces table exists
            result = conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'faces'
            """))
            
            faces_table_exists = result.fetchone() is not None
            
            if not faces_table_exists:
                print("❌ Error: 'faces' table does not exist!")
                print("   Please create the faces table first.")
                sys.exit(1)
        
        # Now perform DDL operations using autocommit connection
        with engine.execution_options(autocommit=True).connect() as ddl_conn:
            try:
                # Add identity_id column if needed
                if 'identity_id' in columns_to_add:
                    print("📦 Adding 'identity_id' column to 'faces' table...")
                    
                    if identities_table_exists:
                        # Add column with foreign key constraint
                        ddl_conn.execute(text("""
                            ALTER TABLE faces 
                            ADD COLUMN identity_id UUID REFERENCES identities(id)
                        """))
                        print("   ✅ Added column with foreign key constraint")
                    else:
                        # Add column without foreign key (will add constraint later)
                        ddl_conn.execute(text("""
                            ALTER TABLE faces 
                            ADD COLUMN identity_id UUID
                        """))
                        print("   ✅ Added column (without foreign key - identities table missing)")
                    
                    # Create index on the column
                    print("📦 Creating index on 'identity_id' column...")
                    ddl_conn.execute(text("""
                        CREATE INDEX IF NOT EXISTS idx_faces_identity_id ON faces(identity_id)
                    """))
                    print("   ✅ Index created")
                
                # Add label_state column if needed
                if 'label_state' in columns_to_add:
                    print("📦 Adding 'label_state' column to 'faces' table...")
                    
                    # First check if the enum type exists
                    with engine.connect() as check_conn:
                        result = check_conn.execute(text("""
                            SELECT EXISTS (
                                SELECT 1 FROM pg_type WHERE typname = 'labelstate'
                            )
                        """))
                        enum_exists = result.fetchone()[0]
                    
                    if not enum_exists:
                        # Create the enum type
                        print("   Creating 'labelstate' enum type...")
                        ddl_conn.execute(text("""
                            CREATE TYPE labelstate AS ENUM (
                                'auto_unknown',
                                'auto_known',
                                'manual_labeled'
                            )
                        """))
                        print("   ✅ Enum type created")
                    
                    # Add column with enum type
                    ddl_conn.execute(text("""
                        ALTER TABLE faces 
                        ADD COLUMN label_state labelstate
                    """))
                    print("   ✅ Added column")
                    
                    # Create index on the column
                    print("📦 Creating index on 'label_state' column...")
                    ddl_conn.execute(text("""
                        CREATE INDEX IF NOT EXISTS idx_faces_label_state ON faces(label_state)
                    """))
                    print("   ✅ Index created")
                
                print("✅ Migration completed successfully!")
                for col in columns_to_add:
                    print(f"   Added column: faces.{col}")
                
            except Exception as e:
                raise e
                
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        print("\nTroubleshooting:")
        print("1. Make sure PostgreSQL is running")
        print("2. Check DATABASE_URL in .env file")
        print("3. Ensure psycopg2-binary is installed: pip install psycopg2-binary")
        print("4. If running in Docker, use: docker-compose exec face_recognition python add_identity_id_column.py")
        sys.exit(1)

if __name__ == "__main__":
    run_migration()

