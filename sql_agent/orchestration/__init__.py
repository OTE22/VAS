"""Orchestrator selection: the built-in LangGraph ReAct loop, or the NeMo
Agent Toolkit adapter over the MCP tools, chosen by ``AGENT_ORCHESTRATOR``.

The selection is a fact reported at startup and on ``system.capabilities``;
a requested orchestrator that cannot run (toolkit not installed) falls back
to LangGraph with a logged reason - never silently, never by trying the
network.
"""
from .nemo_adapter import NemoAgentAdapter, OrchestratorChoice, select_orchestrator  # noqa: F401
