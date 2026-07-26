"""
SQL Intelligence Agent
=======================
An advanced SQL agent using LangChain and LangGraph.
Uses Ollama (llama3.2:3b), PostgreSQL, and ChromaDB for RAG.

Requirements:
    pip install langchain langchain-community langgraph langchain-ollama psycopg2-binary pydantic chromadb sentence-transformers

Usage:
    from sql_agent import SQLIntelligenceAgent

    agent = SQLIntelligenceAgent()
    response = agent.query("Show me all detected faces")
    print(response)

Or run as CLI:
    python -m sql_agent
"""

from .config import Config, config
from .state import AgentState
from .llm import create_llm
from .database import DatabaseManager
from .knowledge_base import SQLKnowledgeBase
from .graph import create_sql_agent
from .agent import SQLIntelligenceAgent
from .tools import prepare_sql_from_llm_response, validate_sql_query, SQLAgentTools

__version__ = "1.0.0"

__all__ = [
    # Configuration
    "Config",
    "config",

    # State
    "AgentState",

    # LLM
    "create_llm",

    # Database
    "DatabaseManager",

    # Knowledge Base
    "SQLKnowledgeBase",

    # Graph
    "create_sql_agent",

    # Agent
    "SQLIntelligenceAgent",

    # Tools
    "prepare_sql_from_llm_response",
    "validate_sql_query",
    "SQLAgentTools",
]
