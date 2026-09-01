"""
Configuration Module
====================
Configuration for the SQL Intelligence Agent.

Every value comes from the central `config.settings`. There is no environment
fallback chain here on purpose: this module used to resolve each value as
`getattr(settings, name) or os.getenv(name, default)`, which meant a setting
that was deliberately empty silently picked up a different value — and the
agent could end up talking to a different database than the API it ships with.
The agent is not a separate application; it must see exactly what the service
sees.
"""

import logging
from dataclasses import dataclass, field
from typing import Tuple

from config import settings

logger = logging.getLogger(__name__)


def agent_db_role_is_dedicated() -> bool:
    """True when generated SQL runs as a role distinct from the application's.

    Read by the production config guard, so the check lives with the logic that
    resolves the credentials rather than being duplicated there.
    """
    agent_user = settings.SQL_AGENT_DB_USER.strip()
    return bool(agent_user) and agent_user != settings.POSTGRES_USER.strip()


def _resolve_agent_db_credentials() -> Tuple[str, str]:
    """Credentials used to execute LLM-generated SQL.

    Prefers the dedicated read-only role. Falls back to the application's role
    so development keeps working, but says so loudly — in that mode the AST
    guard is the only thing standing between generated SQL and a write.

    The password itself (including any SQL_AGENT_DB_PASSWORD_FILE) is resolved
    centrally in config.Settings, so a mounted Docker secret reaches the agent
    and the API through the same code path. An unreadable or empty secret file
    is caught by the production config guard at startup rather than surfacing
    here as an authentication failure on the first question a user asks.
    """
    agent_user = settings.SQL_AGENT_DB_USER.strip()
    if agent_user:
        return agent_user, settings.SQL_AGENT_DB_PASSWORD

    logger.warning(
        "[SQL_AGENT] SQL_AGENT_DB_USER is not set — generated SQL will run as "
        "'%s', which holds write privileges. Set SQL_AGENT_DB_USER to a "
        "read-only role (see db/roles.sql). Production refuses to start "
        "without it.",
        settings.POSTGRES_USER,
    )
    return settings.POSTGRES_USER, settings.POSTGRES_PASSWORD


@dataclass
class Config:
    """Configuration for the SQL Agent.

    default_factory rather than a plain default on every field: a bare default
    is evaluated once when the class body executes, which froze these values at
    import time and made them immune to anything that reconstructs settings.
    """
    # Ollama
    ollama_base_url: str = field(default_factory=lambda: settings.OLLAMA_BASE_URL)
    ollama_model: str = field(default_factory=lambda: settings.OLLAMA_MODEL)
    # SQL-specialist model used only for SQL generation/repair; empty = use ollama_model
    ollama_sql_model: str = field(default_factory=lambda: settings.OLLAMA_SQL_MODEL)
    ollama_temperature: float = field(default_factory=lambda: settings.OLLAMA_TEMPERATURE)
    ollama_timeout: int = field(default_factory=lambda: settings.OLLAMA_TIMEOUT)

    # Development-only hosted provider (NVIDIA NIM). The registry consults
    # is_production and refuses to register it there regardless of these
    # values; the production config guard additionally fails the boot. See
    # the LLM_DEV_PROVIDER block in the central config for the full policy.
    llm_dev_provider: str = field(default_factory=lambda: settings.LLM_DEV_PROVIDER)
    nim_base_url: str = field(default_factory=lambda: settings.NVIDIA_NIM_BASE_URL)
    nim_api_key: str = field(default_factory=lambda: settings.NVIDIA_NIM_API_KEY)
    nim_model: str = field(default_factory=lambda: settings.NVIDIA_NIM_MODEL)
    nim_sql_model: str = field(default_factory=lambda: settings.NVIDIA_NIM_SQL_MODEL)
    nim_timeout: int = field(default_factory=lambda: settings.NVIDIA_NIM_TIMEOUT)
    is_production: bool = field(default_factory=lambda: settings.is_production)

    # PostgreSQL
    db_url: str = field(default_factory=lambda: settings.DATABASE_URL)
    db_host: str = field(default_factory=lambda: settings.DB_HOST)
    db_port: int = field(default_factory=lambda: settings.DB_PORT)
    db_name: str = field(default_factory=lambda: settings.POSTGRES_DB)
    # Credentials for executing LLM-generated SQL.
    #
    # SQL_AGENT_DB_USER is preferred and should be a role with SELECT and
    # nothing else. Falling back to the application's own role means generated
    # SQL runs with write privileges — the AST guard is then the only thing
    # preventing a write, and application code can have bugs where role grants
    # cannot. The production config guard rejects the fallback; see
    # _resolve_agent_db_credentials above for the warning path in development.
    db_user: str = field(default_factory=lambda: _resolve_agent_db_credentials()[0])
    db_password: str = field(default_factory=lambda: _resolve_agent_db_credentials()[1])

    # ChromaDB
    chroma_persist_dir: str = field(default_factory=lambda: settings.CHROMADB_PATH)
    chroma_collection_name: str = field(
        default_factory=lambda: settings.CHROMA_COLLECTION_NAME)

    # RAG
    rag_top_k: int = field(default_factory=lambda: settings.RAG_TOP_K)
    rag_similarity_threshold: float = field(
        default_factory=lambda: settings.RAG_SIMILARITY_THRESHOLD)

    @property
    def db_connection_string(self) -> str:
        """Get database connection string."""
        if self.db_url:
            return self.db_url
        return (f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}")


# Global config instance
config = Config()
