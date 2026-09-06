"""Local OpenAI-compatible provider: vLLM or a local NVIDIA NIM container.

Both speak the OpenAI chat-completions API that ``NIMChatModel`` already
implements for the hosted development endpoint, so this provider reuses
that client with two differences that matter for an air-gapped box:

  * the endpoint is INTERNAL (``LLM_BASE_URL``, e.g. ``http://vllm:8000/v1``),
    which the offline policy verifies at boot;
  * no credential is required. vLLM ignores the header; a local NIM may be
    started with one, so ``LLM_API_KEY`` is optional and never logged.

The registry registers the models here with ``RESTRICTED`` sensitivity
because they run on the host - exactly like Ollama.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import LLMProvider, ModelSpec, ProviderUnavailable
from .nim_provider import NIMChatModel


class OpenAICompatProvider(LLMProvider):
    """Provider adapter with the same contract as OllamaProvider / NIMProvider."""

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str = "", default_temperature: float = 0.1):
        if not (base_url or "").strip():
            raise ProviderUnavailable("LLM_BASE_URL is empty; a local OpenAI-compatible "
                                      "server needs its base URL (e.g. http://vllm:8000/v1).")
        self.base_url = base_url.rstrip("/")
        # vLLM accepts any bearer; sending a placeholder keeps one client path.
        self.api_key = (api_key or "").strip() or "local"
        self.default_temperature = default_temperature

    def build(self, spec: ModelSpec, **overrides: Any):
        response_timeout = float(overrides.pop("timeout", spec.timeout_seconds))
        return NIMChatModel(
            base_url=overrides.pop("base_url", self.base_url),
            model=spec.model_id,
            api_key=self.api_key,
            temperature=float(overrides.pop("temperature", self.default_temperature)),
            timeout_seconds=response_timeout,
        )

    def health_check(self) -> bool:
        """Liveness probe against the models listing. Never raises."""
        try:
            with httpx.Client(timeout=httpx.Timeout(5.0, connect=5.0)) as client:
                response = client.get(f"{self.base_url}/models",
                                      headers={"Authorization": f"Bearer {self.api_key}"})
            return 200 <= response.status_code < 300
        except Exception:
            return False
