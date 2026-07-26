"""
Configuration Module
====================
Configuration settings for the SQL Intelligence Agent.
Uses the main config.py settings with fallback to environment variables.
"""

import os
import sys
from dataclasses import dataclass

# Try to import from main config, fallback to env vars
USE_MAIN_CONFIG = False
main_settings = None
try:
    # Add parent directory to path to import main config
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from config import settings as main_settings
    USE_MAIN_CONFIG = True
except (ImportError, AttributeError) as e:
    # If import fails, use environment variables directly
    USE_MAIN_CONFIG = False
    main_settings = None


@dataclass
class Config:
    """Configuration for the SQL Agent."""
    # Ollama settings - use main config if available, otherwise env vars
    ollama_base_url: str = (
        main_settings.OLLAMA_BASE_URL if USE_MAIN_CONFIG and main_settings else
        os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
    )
    ollama_model: str = (
        main_settings.OLLAMA_MODEL if USE_MAIN_CONFIG and main_settings else
        os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    )
    # SQL-specialist model used only for SQL generation/repair; empty = use ollama_model
    ollama_sql_model: str = (
        getattr(main_settings, "OLLAMA_SQL_MODEL", "") if USE_MAIN_CONFIG and main_settings else
        os.getenv("OLLAMA_SQL_MODEL", "")
    )
    ollama_temperature: float = (
        main_settings.OLLAMA_TEMPERATURE if USE_MAIN_CONFIG and main_settings else
        float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))
    )
    ollama_timeout: int = (
        main_settings.OLLAMA_TIMEOUT if USE_MAIN_CONFIG and main_settings else
        int(os.getenv("OLLAMA_TIMEOUT", "120"))
    )

    # PostgreSQL settings - use main config if available, otherwise env vars
    db_url: str = (
        main_settings.DATABASE_URL if USE_MAIN_CONFIG and main_settings else
        os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:admin@postgres:5432/face_recognition")
    )
    db_host: str = (
        main_settings.DB_HOST if USE_MAIN_CONFIG and main_settings else
        os.getenv("DB_HOST", "postgres")
    )
    db_port: int = (
        main_settings.DB_PORT if USE_MAIN_CONFIG and main_settings else
        int(os.getenv("DB_PORT", "5432"))
    )
    db_name: str = (
        main_settings.POSTGRES_DB if USE_MAIN_CONFIG and main_settings else
        os.getenv("POSTGRES_DB", "face_recognition")
    )
    db_user: str = (
        main_settings.POSTGRES_USER if USE_MAIN_CONFIG and main_settings else
        os.getenv("POSTGRES_USER", "postgres")
    )
    db_password: str = (
        main_settings.POSTGRES_PASSWORD if USE_MAIN_CONFIG and main_settings else
        os.getenv("POSTGRES_PASSWORD", "admin")
    )

    # ChromaDB settings - use main config if available, otherwise env vars
    chroma_persist_dir: str = (
        main_settings.CHROMADB_PATH if USE_MAIN_CONFIG and main_settings else
        os.getenv("CHROMADB_PATH", "./sql_agent/chromadb_data")
    )
    chroma_collection_name: str = (
        main_settings.CHROMA_COLLECTION_NAME if USE_MAIN_CONFIG and main_settings else
        os.getenv("CHROMA_COLLECTION_NAME", "sql_knowledge_base")
    )

    # RAG settings - use main config if available, otherwise env vars
    rag_top_k: int = (
        main_settings.RAG_TOP_K if USE_MAIN_CONFIG and main_settings else
        int(os.getenv("RAG_TOP_K", "5"))
    )
    rag_similarity_threshold: float = (
        main_settings.RAG_SIMILARITY_THRESHOLD if USE_MAIN_CONFIG and main_settings else
        float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.3"))
    )

    @property
    def db_connection_string(self) -> str:
        """Get database connection string."""
        if self.db_url:
            return self.db_url
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


# Global config instance
config = Config()
