"""MCP tool layer for the data agent: narrow, validated, audited, timed.

``tools.MCPToolset`` is the in-process catalogue every orchestrator uses
(the built-in LangGraph loop and the NeMo Agent Toolkit adapter alike);
``server`` exposes the same catalogue over the Model Context Protocol when
the ``mcp`` SDK is installed, bound to an internal address only.

There is deliberately no ``execute_any_sql`` tool. Execution goes through
``database.execute_readonly``, which runs the AST guard, the caller's camera
scope and the read-only role before a single row is read.
"""
from .tools import MCPToolset, ToolContext, ToolError, TOOL_NAMES  # noqa: F401
