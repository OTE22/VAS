"""Ollama adapter.

The only provider implemented, deliberately: this deployment queries biometric
data and the brief's data-sensitivity rules mean prompt text must not leave the
host. Ollama runs locally, so it is the only provider currently permitted for
RESTRICTED content. Adding a hosted provider is a new file implementing
LLMProvider plus a ModelSpec whose max_sensitivity says what it may see — no
change to the agent.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import LLMProvider, ModelSpec, ProviderUnavailable

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    """Builds ChatOllama runnables and probes the Ollama server."""

    name = "ollama"

    def __init__(self, base_url: str, default_temperature: float = 0.1):
        self.base_url = base_url.rstrip("/")
        self.default_temperature = default_temperature

    def build(self, spec: ModelSpec, **overrides: Any) -> Any:
        try:
            from langchain_ollama import ChatOllama
        except ImportError as e:  # pragma: no cover - dependency is pinned
            raise ProviderUnavailable(f"langchain_ollama is not installed: {e}") from e

        return ChatOllama(
            base_url=overrides.pop("base_url", self.base_url),
            model=spec.model_id,
            temperature=overrides.pop("temperature", self.default_temperature),
            # The provider's own timeout. The gateway applies a second,
            # outer deadline — a client that hangs below its socket timeout
            # would otherwise stall the whole request.
            timeout=overrides.pop("timeout", spec.timeout_seconds),
            **overrides,
        )

    def health_check(self) -> bool:
        """True when the Ollama server answers. Never raises."""
        try:
            import urllib.request

            request = urllib.request.Request(f"{self.base_url}/api/tags", method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                return 200 <= response.status < 300
        except Exception as e:
            logger.warning("[LLM] Ollama health check failed: %s: %s", type(e).__name__, e)
            return False

    def list_models(self) -> list:
        """Model names the server actually has pulled.

        Used to tell "model not installed" apart from "provider down" — the
        two look identical from a failed request but need different fixes.
        """
        try:
            import json
            import urllib.request

            request = urllib.request.Request(f"{self.base_url}/api/tags", method="GET")
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read() or b"{}")
            return [m.get("name", "") for m in payload.get("models", [])]
        except Exception as e:
            logger.warning("[LLM] Could not list Ollama models: %s", e)
            return []
