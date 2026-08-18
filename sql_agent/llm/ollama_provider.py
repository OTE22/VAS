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

    # Establishing a TCP connection to a local Ollama is fast; if it has not
    # happened in this long the server is down, and waiting out the much larger
    # response budget to discover that helps nobody.
    CONNECT_TIMEOUT_SECONDS = 20.0

    def __init__(self, base_url: str, default_temperature: float = None):
        from config import settings as _settings
        self.base_url = base_url.rstrip("/")
        # None, not 0.1: the literal default here shadowed the declared
        # OLLAMA_TEMPERATURE setting for every caller that omitted the argument.
        self.default_temperature = (float(_settings.OLLAMA_TEMPERATURE)
                                    if default_temperature is None
                                    else default_temperature)

    @classmethod
    def _client_timeout(cls, response_timeout: float):
        """httpx timeout for the underlying Ollama client.

        ChatOllama accepts NO `timeout` field (langchain_ollama 1.0.x): passing
        one is silently discarded, which is why the configured OLLAMA_TIMEOUT was
        never enforced and a single SQL-generation call was observed running for
        458 seconds against a 120s setting. `client_kwargs` is the supported
        route — it reaches the httpx client, where the timeout is real.

        Connect and read are set separately so a dead server fails fast while a
        slow-but-alive generation still gets its full budget.
        """
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx ships with the ollama client
            return response_timeout
        return httpx.Timeout(
            response_timeout,
            connect=min(cls.CONNECT_TIMEOUT_SECONDS, response_timeout),
        )

    def build(self, spec: ModelSpec, **overrides: Any) -> Any:
        try:
            from langchain_ollama import ChatOllama
        except ImportError as e:  # pragma: no cover - dependency is pinned
            raise ProviderUnavailable(f"langchain_ollama is not installed: {e}") from e

        response_timeout = float(overrides.pop("timeout", spec.timeout_seconds))
        # Caller-supplied client_kwargs win, but the timeout is filled in when
        # absent so no code path can accidentally build an unbounded client.
        client_kwargs = dict(overrides.pop("client_kwargs", None) or {})
        client_kwargs.setdefault("timeout", self._client_timeout(response_timeout))

        return ChatOllama(
            base_url=overrides.pop("base_url", self.base_url),
            model=spec.model_id,
            temperature=overrides.pop("temperature", self.default_temperature),
            # NOT `timeout=` — ChatOllama has no such field and drops it
            # silently. See _client_timeout.
            client_kwargs=client_kwargs,
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
