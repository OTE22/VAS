"""NVIDIA NIM provider — DEVELOPMENT ONLY.

An OpenAI-compatible adapter for build.nvidia.com's free hosted endpoint
(https://integrate.api.nvidia.com/v1), so a developer can judge SQL-generation
quality against a stronger model than the local Ollama ones.

This provider must never serve production traffic: it sends the database
schema and every user question to an external service, and this system
queries biometric data. The refusal is layered and none of the layers is this
file trusting itself:

  * build_default_registry() does not register any NIM ModelSpec when
    cfg.is_production — the router cannot pick a model that does not exist;
  * the production config guard fails the boot (exit 78) when
    LLM_DEV_PROVIDER is set at all;
  * LLM_DEV_PROVIDER is SECURITY_CRITICAL, so the admin settings API cannot
    persist it for a later boot to pick up.

Implemented directly on httpx against /chat/completions rather than adding
langchain-openai: the dependency would ship in the production image to serve
a feature production is forbidden from using. langchain_core is already here
(transitively through langchain-ollama), and BaseChatModel is a Runnable, so
the gateway's _InstrumentedModel wraps this exactly like ChatOllama.
"""

import json
import logging
from typing import Any, Iterator, List, Optional

import httpx
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from .base import ModelSpec, ProviderUnavailable

logger = logging.getLogger(__name__)

# LangChain message types -> OpenAI chat roles. Anything unknown becomes
# "user" rather than raising: a lost role is a worse prompt, not a crash.
_ROLE = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}


def _to_openai_messages(messages: List[BaseMessage]) -> List[dict]:
    converted = []
    for message in messages:
        content = message.content
        if not isinstance(content, str):
            # Multimodal blocks are never produced by this agent; flatten
            # defensively instead of sending a shape NIM may reject.
            content = json.dumps(content)
        converted.append({"role": _ROLE.get(message.type, "user"),
                          "content": content})
    return converted


class NIMChatModel(BaseChatModel):
    """Minimal OpenAI-compatible chat model over httpx.

    Only what the SQL agent's chains actually use: invoke (via _generate) and
    stream (via _stream). Token usage is reported through usage_metadata in
    the shape the gateway's accounting already reads first.
    """

    base_url: str
    model: str
    api_key: str
    temperature: float = 0.1
    timeout_seconds: float = 60.0

    @property
    def _llm_type(self) -> str:
        return "nvidia-nim"

    @property
    def _identifying_params(self) -> dict:
        # Never the key.
        return {"base_url": self.base_url, "model": self.model}

    # ---- shared plumbing --------------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def _payload(self, messages: List[BaseMessage],
                 stop: Optional[List[str]], stream: bool, **kwargs: Any) -> dict:
        payload = {
            "model": self.model,
            "messages": _to_openai_messages(messages),
            "temperature": self.temperature,
            "stream": stream,
        }
        if stop:
            payload["stop"] = stop
        # Callers may pass OpenAI-shaped extras (max_tokens etc.) through
        # bind(); unknown LangChain-internal kwargs must not reach the wire.
        # tools/tool_choice are how native function calling reaches the
        # wire. NIM is OpenAI-compatible and returns real tool_calls, so
        # the agent can look things up mid-turn instead of guessing.
        # Without these in the allowlist the payload was silently dropped
        # and every model looked like it did not support tools.
        for key in ("max_tokens", "top_p", "seed", "response_format",
                    "tools", "tool_choice"):
            if key in kwargs and kwargs[key] is not None:
                payload[key] = kwargs[key]
        return payload

    def _timeout(self) -> httpx.Timeout:
        # Same shape as the Ollama adapter: bounded connect phase so an
        # unreachable endpoint fails in seconds, not the whole response budget.
        return httpx.Timeout(self.timeout_seconds,
                             connect=min(20.0, self.timeout_seconds))

    # ---- invoke -----------------------------------------------------------

    def _generate(self, messages: List[BaseMessage],
                  stop: Optional[List[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None,
                  **kwargs: Any) -> ChatResult:
        with httpx.Client(timeout=self._timeout()) as client:
            response = client.post(f"{self.base_url}/chat/completions",
                                   headers=self._headers(),
                                   json=self._payload(messages, stop, False, **kwargs))
            response.raise_for_status()
            data = response.json()

        choice = (data.get("choices") or [{}])[0]
        raw_message = choice.get("message") or {}
        content = raw_message.get("content") or ""
        usage = data.get("usage") or {}

        # NATIVE FUNCTION CALLING. When the model calls a tool the reply has
        # an empty `content` and a populated `tool_calls`; reading content
        # alone silently threw the call away and made every model look like
        # it did not support tools. Carried in additional_kwargs, which is
        # where LangChain callers (and parse_tool_response) look.
        extra = {}
        tool_calls = raw_message.get("tool_calls")
        if tool_calls:
            extra["tool_calls"] = tool_calls

        message = AIMessage(
            content=content,
            additional_kwargs=extra,
            usage_metadata={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
            response_metadata={"model_name": data.get("model", self.model),
                               "finish_reason": choice.get("finish_reason")},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    # ---- stream -----------------------------------------------------------

    def _stream(self, messages: List[BaseMessage],
                stop: Optional[List[str]] = None,
                run_manager: Optional[CallbackManagerForLLMRun] = None,
                **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        with httpx.Client(timeout=self._timeout()) as client:
            with client.stream("POST", f"{self.base_url}/chat/completions",
                               headers=self._headers(),
                               json=self._payload(messages, stop, True, **kwargs)) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    piece = line[len("data:"):].strip()
                    if piece == "[DONE]":
                        break
                    try:
                        event = json.loads(piece)
                    except ValueError:
                        continue   # a torn SSE frame is dropped, not fatal
                    delta = ((event.get("choices") or [{}])[0]
                             .get("delta") or {})
                    token = delta.get("content")
                    if not token:
                        continue
                    chunk = ChatGenerationChunk(
                        message=AIMessageChunk(content=token))
                    if run_manager:
                        run_manager.on_llm_new_token(token, chunk=chunk)
                    yield chunk


class NIMProvider:
    """Provider adapter with the same contract as OllamaProvider."""

    name = "nim"

    def __init__(self, base_url: str, api_key: str,
                 default_temperature: float = 0.1):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_temperature = default_temperature

    def build(self, spec: ModelSpec, **overrides: Any):
        if not self.api_key:
            raise ProviderUnavailable(
                "NVIDIA_NIM_API_KEY is empty — get a free key at "
                "build.nvidia.com and set it in the development environment.")
        response_timeout = float(overrides.pop("timeout", spec.timeout_seconds))
        return NIMChatModel(
            base_url=overrides.pop("base_url", self.base_url),
            model=spec.model_id,
            api_key=self.api_key,
            temperature=float(overrides.pop("temperature",
                                            self.default_temperature)),
            timeout_seconds=response_timeout,
        )

    def health_check(self) -> bool:
        """Cheap liveness probe against the models listing. Never raises."""
        try:
            with httpx.Client(timeout=httpx.Timeout(5.0, connect=5.0)) as client:
                response = client.get(f"{self.base_url}/models",
                                      headers=self._auth_only())
            return 200 <= response.status_code < 300
        except Exception:
            return False

    def _auth_only(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}
