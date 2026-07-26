"""
LLM Module
==========
LLM setup and configuration for the SQL Intelligence Agent.
"""

from langchain_ollama import ChatOllama
from .config import config


def create_llm() -> ChatOllama:
    """General-purpose LLM: chat, intent classification, language normalization, analysis."""
    return ChatOllama(
        base_url=config.ollama_base_url,
        model=config.ollama_model,
        temperature=config.ollama_temperature,
        timeout=config.ollama_timeout,
    )


def create_sql_llm() -> ChatOllama:
    """SQL-specialist LLM used only for SQL generation and repair steps.

    Falls back to the general model when OLLAMA_SQL_MODEL is not configured.
    """
    return ChatOllama(
        base_url=config.ollama_base_url,
        model=config.ollama_sql_model or config.ollama_model,
        temperature=config.ollama_temperature,
        timeout=config.ollama_timeout,
    )
