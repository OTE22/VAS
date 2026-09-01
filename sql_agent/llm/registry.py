"""Model registry and routing.

Which model serves a task was previously implicit: two factory functions, each
hardcoding a config value. Routing now takes the task, the capabilities it
needs and the sensitivity of the content, and returns an ordered list of
candidates. The first is preferred; the rest are fallbacks.

Fallback is ordered but never silent — the gateway records which model actually
answered, because "the SQL specialist was down so a general model wrote your
query" changes how much you should trust the result.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .base import Capability, DataSensitivity, ModelSpec, TaskType

logger = logging.getLogger(__name__)

# Tasks that need a model good at SQL, in preference order.
_SQL_TASKS = frozenset({TaskType.SQL_GENERATION, TaskType.SQL_MODIFICATION,
                        TaskType.SQL_REPAIR})


class ModelRegistry:
    """The models this deployment may use, and the rules for choosing one."""

    def __init__(self):
        self._specs: Dict[str, ModelSpec] = {}
        self._task_preferences: Dict[TaskType, List[str]] = {}

    # ---------------------------------------------------------- registration

    def register(self, spec: ModelSpec) -> None:
        self._specs[spec.model_id] = spec
        logger.debug("[LLM] Registered %s/%s", spec.provider, spec.model_id)

    def prefer(self, task: TaskType, model_ids: List[str]) -> None:
        """Set the preference order for a task, best first."""
        self._task_preferences[task] = list(model_ids)

    def get(self, model_id: str) -> Optional[ModelSpec]:
        return self._specs.get(model_id)

    def all(self) -> List[ModelSpec]:
        return list(self._specs.values())

    # ------------------------------------------------------------- routing

    def route(
        self,
        task: TaskType,
        *,
        sensitivity: DataSensitivity = DataSensitivity.RESTRICTED,
        required: Optional[List[Capability]] = None,
        requested_model: Optional[str] = None,
    ) -> List[ModelSpec]:
        """Candidate models for `task`, best first.

        An explicitly requested model is honoured only if it clears the same
        checks — a user preference must not widen what the data policy allows.
        """
        required = required or []

        def eligible(spec: ModelSpec) -> bool:
            if not spec.available:
                return False
            if not spec.permits(sensitivity):
                # e.g. a hosted model asked to see RESTRICTED content.
                logger.debug(
                    "[LLM] %s excluded: max sensitivity %s < required %s",
                    spec.model_id, spec.max_sensitivity.value, sensitivity.value,
                )
                return False
            return all(spec.supports(c) for c in required)

        if requested_model:
            spec = self._specs.get(requested_model)
            if spec and eligible(spec):
                others = [s for s in self._ordered(task) if s.model_id != spec.model_id
                          and eligible(s)]
                return [spec] + others
            logger.warning(
                "[LLM] Requested model %s is not eligible for this task; "
                "falling back to policy order.", requested_model,
            )

        return [spec for spec in self._ordered(task) if eligible(spec)]

    def _ordered(self, task: TaskType) -> List[ModelSpec]:
        preferred_ids = self._task_preferences.get(task, [])
        ordered = [self._specs[m] for m in preferred_ids if m in self._specs]
        seen = {spec.model_id for spec in ordered}
        ordered.extend(spec for spec in self._specs.values() if spec.model_id not in seen)
        return ordered


def build_default_registry(cfg) -> ModelRegistry:
    """Registry describing the models this deployment is configured with.

    Both are local Ollama models, so both may see RESTRICTED content. A hosted
    model would be registered with max_sensitivity=INTERNAL or PUBLIC and would
    then be excluded from routing for biometric queries automatically.
    """
    registry = ModelRegistry()

    general_id = cfg.ollama_model
    sql_id = cfg.ollama_sql_model or cfg.ollama_model

    general_caps = frozenset({
        Capability.STREAMING,
        Capability.JSON_MODE,
    })

    registry.register(ModelSpec(
        provider="ollama",
        model_id=general_id,
        display_name=f"{general_id} (general)",
        capabilities=general_caps,
        context_tokens=8192,
        max_sensitivity=DataSensitivity.RESTRICTED,  # runs locally
        timeout_seconds=float(cfg.ollama_timeout),
    ))

    if sql_id != general_id:
        registry.register(ModelSpec(
            provider="ollama",
            model_id=sql_id,
            display_name=f"{sql_id} (SQL specialist)",
            capabilities=general_caps,
            context_tokens=8192,
            max_sensitivity=DataSensitivity.RESTRICTED,
            timeout_seconds=float(cfg.ollama_timeout),
        ))

    # ---- development-only hosted provider (NVIDIA NIM) --------------------
    #
    # `cfg.is_production` is the gate, not the API key: a key present in a
    # production environment registers NOTHING, so the router cannot select a
    # model that would send schema and question text off-box. The production
    # config guard independently fails the boot when LLM_DEV_PROVIDER is set,
    # and the flag is SECURITY_CRITICAL so it cannot arrive via the admin
    # settings API. Three layers, none trusting the others.
    #
    # In development the NIM specs are registered at RESTRICTED and preferred
    # over Ollama: a development database holds development data, and the
    # whole point of the provider is to compare generated SQL against a
    # stronger model. Ollama stays registered as the fallback, so the agent
    # keeps answering when the endpoint or key is misbehaving.
    nim_enabled = (
        not getattr(cfg, "is_production", True)
        and str(getattr(cfg, "llm_dev_provider", "") or "").strip().lower() == "nim"
        and bool(str(getattr(cfg, "nim_api_key", "") or "").strip())
    )
    nim_general = nim_sql = None
    if nim_enabled:
        nim_general = cfg.nim_model
        nim_sql = cfg.nim_sql_model or cfg.nim_model
        registry.register(ModelSpec(
            provider="nim",
            model_id=nim_general,
            display_name=f"{nim_general} (NIM, development)",
            capabilities=general_caps,
            context_tokens=32768,
            max_sensitivity=DataSensitivity.RESTRICTED,  # dev data only; see above
            timeout_seconds=float(cfg.nim_timeout),
        ))
        if nim_sql != nim_general:
            registry.register(ModelSpec(
                provider="nim",
                model_id=nim_sql,
                display_name=f"{nim_sql} (NIM SQL specialist, development)",
                capabilities=general_caps,
                context_tokens=32768,
                max_sensitivity=DataSensitivity.RESTRICTED,
                timeout_seconds=float(cfg.nim_timeout),
            ))

    # SQL work prefers the specialist and falls back to the general model;
    # with NIM enabled the hosted models come first and Ollama remains the
    # local fallback.
    sql_order = [sql_id, general_id]
    chat_order = [general_id]
    if nim_enabled:
        sql_order = [nim_sql, nim_general] + sql_order
        chat_order = [nim_general] + chat_order
    # First occurrence wins: when the specialist IS the general model the
    # naive lists repeat an id, and a repeated id would yield the same spec
    # twice in route()'s candidates.
    sql_order = list(dict.fromkeys(sql_order))
    chat_order = list(dict.fromkeys(chat_order))

    for task in _SQL_TASKS:
        registry.prefer(task, sql_order)

    for task in (TaskType.CHAT, TaskType.INTENT, TaskType.NORMALIZE,
                 TaskType.EXPLANATION):
        registry.prefer(task, chat_order)

    return registry
