"""NeMo Agent Toolkit adapter: a ReAct agent over the MCP tool catalogue.

The built-in LangGraph loop (``sql_agent/graph.py``) remains the default
orchestrator and is untouched. When ``AGENT_ORCHESTRATOR=nemo`` and the
NVIDIA NeMo Agent Toolkit (``nvidia-nat`` with its LangChain plugin) is
importable, this adapter builds the toolkit's ReAct agent whose only tools
are the catalogue in ``sql_agent/mcp`` - so the model can search the schema,
retrieve verified examples, validate and execute SQL through the read-only
gate, and compute analytics, and can never touch the database directly.

The toolkit's Python API is versioned by NVIDIA (1.8: ``nat.plugins.langchain
.agent.react_agent``); it is isolated behind two seams (``_probe_toolkit``
and ``_default_builder``) that a test can replace with fakes. If either
fails, ``select_orchestrator`` reports LangGraph with the reason and nothing
changes for the user.

Tool calls made by the agent carry the caller's ``ToolContext`` (user id,
role, camera scope) from the API layer; the model cannot widen it.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..mcp.tools import MCPToolset, ToolContext

logger = logging.getLogger(__name__)

LANGGRAPH = "langgraph"
NEMO = "nemo"

# The tools a data question needs. Kept SHORT on purpose: a small model
# given thirteen tools re-ran schema search and retrieval until the
# recursion limit without ever executing (2026-09-06, llama-3.2-11b); with
# the five that matter it reaches the query.
DEFAULT_TOOLS = (
    "vanna.retrieve_sql_examples", "vanna.retrieve_ddl",
    "sql.validate", "database.execute_readonly",
    "analytics.percentage_change",
)

SYSTEM_PROMPT = (
    "You are a data analyst for a surveillance system. Answer only from tool results. "
    "Do these steps ONCE each, in order: (1) vanna_retrieve_sql_examples with the question; "
    "(2) write ONE PostgreSQL SELECT, copying the closest example and changing only names or dates; "
    "(3) database_execute_readonly with that SQL; (4) give the Final Answer with the figures from the rows. "
    "Do not call the same tool twice with the same input. Never invent tables or numbers. "
    "Names of people and cameras are copied exactly. If a tool refuses a query, fix the query; "
    "never work around the refusal."
)


@dataclass(frozen=True)
class OrchestratorChoice:
    name: str
    reason: str
    available: bool

    def as_dict(self) -> Dict[str, Any]:
        return {"orchestrator": self.name, "reason": self.reason, "available": self.available}


def _probe_toolkit() -> Optional[str]:
    """The installed toolkit's version (with its LangChain agent plugin), or None."""
    try:
        importlib.import_module("nat.plugins.langchain.agent.react_agent.agent")
    except ImportError:
        return None
    try:
        from importlib.metadata import version
        return version("nvidia-nat")
    except Exception:
        return "unknown"


def select_orchestrator(config, *, probe: Callable[[], Optional[str]] = _probe_toolkit) -> OrchestratorChoice:
    """What runs this deployment's data turns, and why."""
    wanted = str(getattr(config, "agent_orchestrator", LANGGRAPH) or LANGGRAPH).strip().lower()
    if wanted not in (LANGGRAPH, NEMO):
        return OrchestratorChoice(LANGGRAPH, f"AGENT_ORCHESTRATOR={wanted!r} is unknown; using the built-in loop", True)
    if wanted == LANGGRAPH:
        return OrchestratorChoice(LANGGRAPH, "configured", True)
    version = probe()
    if version is None:
        logger.warning("[ORCHESTRATOR] AGENT_ORCHESTRATOR=nemo but the NeMo Agent Toolkit (nvidia-nat with "
                       "the langchain plugin) is not installed; the built-in LangGraph loop runs this deployment")
        return OrchestratorChoice(LANGGRAPH, "nemo requested but the toolkit is not installed", True)
    return OrchestratorChoice(NEMO, f"NeMo Agent Toolkit {version}", True)


class NemoAgentAdapter:
    """Runs a question through a NeMo ReAct agent bound to the MCP toolset.

    ``builder(llm, tool_functions, system_prompt)`` must return an object with
    ``run(question: str) -> str``. The default builder uses the toolkit's
    ReAct agent; tests inject a fake. ``llm`` is the deployment's chat model
    from the existing gateway (local in production).
    """

    def __init__(self, toolset: MCPToolset, llm: Any, *, tools: Optional[List[str]] = None,
                 builder: Optional[Callable[..., Any]] = None, max_iterations: int = 8,
                 native_tool_calling: bool = True, detailed_logs: bool = False):
        self.toolset = toolset
        self.llm = llm
        self.tool_names = [t for t in (tools or DEFAULT_TOOLS) if t in toolset.names()]
        self.builder = builder or self._default_builder
        self.max_iterations = max_iterations
        # Native tool calling uses the model's own function-calling API; the
        # text ReAct format depends on the model reproducing a scaffold
        # exactly, which small models often fail to do.
        self.native_tool_calling = native_tool_calling
        self.detailed_logs = detailed_logs
        self.calls: List[Dict[str, Any]] = []

    # ----- tool functions the agent sees ------------------------------------
    def tool_functions(self, ctx: ToolContext) -> List[Callable[..., Dict[str, Any]]]:
        functions = []
        for spec in self.toolset.specs():
            if spec.name not in self.tool_names:
                continue

            def _call(arguments: Optional[Dict[str, Any]] = None, *, _name=spec.name, **kwargs) -> Dict[str, Any]:
                payload = dict(arguments or {})
                payload.update(kwargs)
                result = self.toolset.call(_name, payload, ctx)
                self.calls.append({"tool": _name, "status": result.get("status"), "error_code": result.get("error_code")})
                return result

            _call.__name__ = spec.name.replace(".", "_")
            _call.__doc__ = spec.description
            _call.mcp_name = spec.name
            _call.input_schema = spec.input_model.model_json_schema()
            _call.args_model = spec.input_model
            functions.append(_call)
        return functions

    # ----- run --------------------------------------------------------------
    def run(self, question: str, ctx: ToolContext) -> Dict[str, Any]:
        self.calls = []
        if self.builder is self._default_builder:
            agent = self.builder(self.llm, self.tool_functions(ctx), SYSTEM_PROMPT,
                                 max_iterations=self.max_iterations,
                                 native_tool_calling=self.native_tool_calling,
                                 detailed_logs=self.detailed_logs)
        else:
            agent = self.builder(self.llm, self.tool_functions(ctx), SYSTEM_PROMPT)
        answer = agent.run(question)
        executed = [c for c in self.calls if c["tool"] == "database.execute_readonly" and c["status"] == "ok"]
        return {"response": str(answer), "tools_called": [c["tool"] for c in self.calls],
                "executed_queries": len(executed), "orchestrator": NEMO}

    # ----- the toolkit ------------------------------------------------------
    @staticmethod
    def _default_builder(llm, tool_functions, system_prompt, max_iterations: int = 8,
                         native_tool_calling: bool = True, detailed_logs: bool = False):
        """Build the toolkit's ReAct agent (nvidia-nat 1.8, LangChain plugin).
        Isolated so a toolkit API change is one function to update; raises
        ImportError when the toolkit is absent."""
        try:
            from nat.plugins.langchain.agent.react_agent.agent import (  # type: ignore
                ReActAgentGraph, ReActGraphState, create_react_agent_prompt)
        except ImportError as e:
            raise ImportError("NeMo Agent Toolkit (nvidia-nat[langchain]) is not installed") from e
        from types import SimpleNamespace
        from langchain_core.tools import StructuredTool

        tools = []
        for fn in tool_functions:
            tools.append(StructuredTool.from_function(
                func=fn, name=fn.__name__, description=(fn.__doc__ or fn.mcp_name)[:1000],
                args_schema=getattr(fn, "args_model", None)))
        # The toolkit's own system prompt carries the ReAct scaffolding
        # ({tools}, {tool_names}); the deployment's instructions ride along.
        prompt = create_react_agent_prompt(SimpleNamespace(system_prompt=None,
                                                           additional_instructions=system_prompt))
        return _ReActRunner(ReActAgentGraph, ReActGraphState, llm, prompt, tools, max_iterations,
                            native_tool_calling, detailed_logs)


class _ReActRunner:
    """Thin wrapper: the toolkit graph exposes an async interface."""

    def __init__(self, graph_cls, state_cls, llm, prompt, tools, max_iterations: int,
                 native_tool_calling: bool = True, detailed_logs: bool = False):
        self.graph_cls = graph_cls
        self.state_cls = state_cls
        self.llm = llm
        self.prompt = prompt
        self.tools = tools
        self.max_iterations = max_iterations
        self.native_tool_calling = native_tool_calling
        self.detailed_logs = detailed_logs

    async def _arun(self, question: str) -> str:
        from langchain_core.messages import HumanMessage
        graph = await self.graph_cls(llm=self.llm, prompt=self.prompt, tools=self.tools,
                                     detailed_logs=self.detailed_logs,
                                     use_native_tool_calling=self.native_tool_calling).build_graph()
        state = self.state_cls(messages=[HumanMessage(content=question)])
        out = await graph.ainvoke(state, config={"recursion_limit": (self.max_iterations + 1) * 2})
        messages = out.get("messages") if isinstance(out, dict) else getattr(out, "messages", [])
        final = getattr(out, "final_answer", None) if not isinstance(out, dict) else out.get("final_answer")
        if final:
            return str(final)
        return str(messages[-1].content) if messages else ""

    def run(self, question: str) -> str:
        import asyncio
        import concurrent.futures
        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
        if not running:
            return asyncio.run(self._arun(question))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(self._arun(question))).result()
