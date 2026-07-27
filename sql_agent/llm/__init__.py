"""
LLM Module
==========
Provider-independent LLM access for the SQL Intelligence Agent.

Was a 31-line module constructing ChatOllama directly in two factories, which
welded the provider into the call path and left nowhere to put timeouts,
retries, fallback or token accounting.

The public API is unchanged — create_llm() and create_sql_llm() still return
something the graph nodes can compose into LCEL chains — so no call site moved.
What changed is what happens behind them: routing by task and data
sensitivity, circuit breaking, jittered retries, and a record of which model
actually answered.

Adding a provider is a new adapter implementing LLMProvider plus a ModelSpec.
Nothing in the agent changes.
"""

import logging

from .base import (
    Capability,
    DataSensitivity,
    LLMCallRecord,
    LLMProvider,
    ModelSpec,
    ProviderUnavailable,
    TaskType,
    TokenUsage,
)
from .gateway import CircuitBreaker, LLMGateway, UsageLedger
from .ollama_provider import OllamaProvider
from .registry import ModelRegistry, build_default_registry

logger = logging.getLogger(__name__)

_gateway = None


def get_gateway():
    """The process-wide gateway, built on first use."""
    global _gateway
    if _gateway is None:
        from ..config import config

        registry = build_default_registry(config)
        providers = {
            "ollama": OllamaProvider(
                base_url=config.ollama_base_url,
                default_temperature=config.ollama_temperature,
            )
        }
        _gateway = LLMGateway(registry, providers)
        logger.info(
            "[LLM] Gateway ready: %d model(s), providers=%s",
            len(registry.all()), sorted(providers),
        )
    return _gateway


def reset_gateway():
    """Drop the cached gateway. For tests and config reloads."""
    global _gateway
    _gateway = None


def create_llm(task: TaskType = TaskType.CHAT, **overrides):
    """General-purpose model: chat, intent classification, normalization, analysis."""
    return get_gateway().build_for(task, **overrides)


def create_sql_llm(task: TaskType = TaskType.SQL_GENERATION, **overrides):
    """SQL-specialist model, falling back to the general model when absent.

    The fallback is recorded rather than silent: which model wrote a query
    changes how much the result should be trusted.
    """
    return get_gateway().build_for(task, **overrides)


__all__ = [
    "Capability",
    "CircuitBreaker",
    "DataSensitivity",
    "LLMCallRecord",
    "LLMGateway",
    "LLMProvider",
    "ModelRegistry",
    "ModelSpec",
    "OllamaProvider",
    "ProviderUnavailable",
    "TaskType",
    "TokenUsage",
    "UsageLedger",
    "build_default_registry",
    "create_llm",
    "create_sql_llm",
    "get_gateway",
    "reset_gateway",
]
