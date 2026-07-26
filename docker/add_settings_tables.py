"""
Migration script to add Settings and SettingsAuditLog tables
This creates the database schema for the settings management system
"""
import os
import sys

# Add parent directory to path (docker folder is one level down)
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, parent_dir)

from sqlalchemy import create_engine, text
from config import settings

def run_migration():
    """Create Settings and SettingsAuditLog tables if they don't exist"""
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
        
        # Check existing tables
        with engine.connect() as conn:
            # Check if settings table exists
            result = conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'settings'
            """))
            settings_table_exists = result.fetchone() is not None
            
            # Check if settings_audit_log table exists
            result = conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'settings_audit_log'
            """))
            audit_log_table_exists = result.fetchone() is not None
            
            # Check if users table exists (required for foreign key)
            result = conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'users'
            """))
            users_table_exists = result.fetchone() is not None
            
            if settings_table_exists and audit_log_table_exists:
                print("✅ Both tables already exist!")
                print("   - settings: ✅")
                print("   - settings_audit_log: ✅")
                print("   Skipping migration.")
                return
            
            if not users_table_exists:
                print("⚠️  Warning: 'users' table does not exist!")
                print("   The foreign key constraint will be created without reference.")
                print("   You may need to create the users table first.")
        
        # Now perform DDL operations using autocommit connection
        with engine.execution_options(autocommit=True).connect() as ddl_conn:
            try:
                # Create settings table if needed
                if not settings_table_exists:
                    print("📦 Creating 'settings' table...")
                    
                    ddl_conn.execute(text("""
                        CREATE TABLE settings (
                            id SERIAL PRIMARY KEY,
                            key VARCHAR(255) UNIQUE NOT NULL,
                            value TEXT,
                            value_type VARCHAR(50) NOT NULL DEFAULT 'string',
                            category VARCHAR(100) NOT NULL,
                            description TEXT,
                            is_sensitive BOOLEAN NOT NULL DEFAULT FALSE,
                            is_readonly BOOLEAN NOT NULL DEFAULT FALSE,
                            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                    """))
                    print("   ✅ Table created")
                    
                    # Create indexes
                    print("📦 Creating indexes on 'settings' table...")
                    ddl_conn.execute(text("""
                        CREATE INDEX IF NOT EXISTS idx_setting_key ON settings(key)
                    """))
                    ddl_conn.execute(text("""
                        CREATE INDEX IF NOT EXISTS idx_setting_category ON settings(category)
                    """))
                    print("   ✅ Indexes created")
                else:
                    print("✅ 'settings' table already exists")
                
                # Create settings_audit_log table if needed
                if not audit_log_table_exists:
                    print("📦 Creating 'settings_audit_log' table...")
                    
                    # Create table with foreign key if users table exists
                    if users_table_exists:
                        ddl_conn.execute(text("""
                            CREATE TABLE settings_audit_log (
                                id SERIAL PRIMARY KEY,
                                setting_key VARCHAR(255) NOT NULL,
                                old_value TEXT,
                                new_value TEXT,
                                value_type VARCHAR(50) NOT NULL,
                                changed_by_user_id INTEGER REFERENCES users(id),
                                changed_by_username VARCHAR(100),
                                change_reason TEXT,
                                ip_address VARCHAR(45),
                                user_agent TEXT,
                                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                            )
                        """))
                    else:
                        ddl_conn.execute(text("""
                            CREATE TABLE settings_audit_log (
                                id SERIAL PRIMARY KEY,
                                setting_key VARCHAR(255) NOT NULL,
                                old_value TEXT,
                                new_value TEXT,
                                value_type VARCHAR(50) NOT NULL,
                                changed_by_user_id INTEGER,
                                changed_by_username VARCHAR(100),
                                change_reason TEXT,
                                ip_address VARCHAR(45),
                                user_agent TEXT,
                                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                            )
                        """))
                    print("   ✅ Table created")
                    
                    # Create indexes
                    print("📦 Creating indexes on 'settings_audit_log' table...")
                    ddl_conn.execute(text("""
                        CREATE INDEX IF NOT EXISTS idx_settings_audit_setting 
                        ON settings_audit_log(setting_key, created_at)
                    """))
                    ddl_conn.execute(text("""
                        CREATE INDEX IF NOT EXISTS idx_settings_audit_user 
                        ON settings_audit_log(changed_by_user_id, created_at)
                    """))
                    ddl_conn.execute(text("""
                        CREATE INDEX IF NOT EXISTS idx_settings_audit_created 
                        ON settings_audit_log(created_at)
                    """))
                    print("   ✅ Indexes created")
                else:
                    print("✅ 'settings_audit_log' table already exists")
                
                print("\n✅ Migration completed successfully!")
                if not settings_table_exists:
                    print("   ✅ Created table: settings")
                if not audit_log_table_exists:
                    print("   ✅ Created table: settings_audit_log")
                
            except Exception as e:
                raise e
                
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        print("\nTroubleshooting:")
        print("1. Make sure PostgreSQL is running")
        print("2. Check DATABASE_URL in .env file")
        print("3. Ensure psycopg2-binary is installed: pip install psycopg2-binary")
        print("4. If running in Docker (CPU version), use:")
        print("   docker-compose -f docker/docker-compose.cpu.yml exec face_recognition python docker/add_settings_tables.py")
        print("   Or using container name: docker exec face_recognition_api python docker/add_settings_tables.py")
        print("   Or from project root: python docker/add_settings_tables.py")
        print("   For GPU version: docker-compose -f docker/docker-compose.gpu.yml exec face_recognition python docker/add_settings_tables.py")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    run_migration()

