"""
Simple migration script to create User, UserPipelineAccess, and ChatbotAuditLog tables
This uses SQLAlchemy's create_all which is simpler than Alembic for initial setup

NOTE: This script is kept for manual migrations. Automatic migrations now run at startup
via Alembic (see backend/utils/migrations.py and backend/lifespan.py)
"""
import os
import sys
import asyncio

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import create_engine, text
from db_models import Base, User, UserPipelineAccess, ChatbotAuditLog
from config import settings

def run_migration():
    """Create new tables using SQLAlchemy"""
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
        
        # Check which tables already exist
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name IN ('users', 'user_pipeline_access', 'chatbot_audit_log')
            """))
            existing_tables = [row[0] for row in result]
            
            tables_to_create = []
            if 'users' not in existing_tables:
                tables_to_create.append(('users', User.__table__))
            if 'user_pipeline_access' not in existing_tables:
                tables_to_create.append(('user_pipeline_access', UserPipelineAccess.__table__))
            if 'chatbot_audit_log' not in existing_tables:
                tables_to_create.append(('chatbot_audit_log', ChatbotAuditLog.__table__))
            
            if not tables_to_create:
                print("✅ All tables already exist!")
                print("   - users")
                print("   - user_pipeline_access")
                print("   - chatbot_audit_log")
                print("   Skipping migration.")
                return
        
        # Create only the missing tables
        print("📦 Creating new tables...")
        tables = [table for _, table in tables_to_create]
        Base.metadata.create_all(engine, tables=tables)
        
        print("✅ Migration completed successfully!")
        print("   Created tables:")
        for table_name, _ in tables_to_create:
            print(f"   - {table_name}")
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        print("\nTroubleshooting:")
        print("1. Make sure PostgreSQL is running")
        print("2. Check DATABASE_URL in .env file")
        print("3. Ensure psycopg2-binary is installed: pip install psycopg2-binary")
        sys.exit(1)

if __name__ == "__main__":
    run_migration()

