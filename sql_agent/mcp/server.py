"""Serve the tool catalogue over the Model Context Protocol - internal only.

    python -m sql_agent.mcp.server                 # stdio (for a local agent host)
    python -m sql_agent.mcp.server --http          # streamable HTTP on 127.0.0.1:9901/mcp

Works with the ``mcp`` Python SDK 2.x (``mcp.server.MCPServer``) and 1.x
(``mcp.server.fastmcp.FastMCP``). Without the SDK this module still imports
(the in-process ``MCPToolset`` is what the orchestrators use) and ``main``
exits with a clear message. The HTTP transport refuses to bind a
non-internal address, so the tools can never be reached from outside the
Docker network by configuration mistake.

Every call carries a ``ToolContext`` built from the transport's caller
identity; there is no way to pass a scope through the tool arguments.
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict, Optional

from .tools import MCPToolset, ToolContext

logger = logging.getLogger(__name__)

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 9901
MCP_PATH = "/mcp"


def build_toolset(config=None, *, user_id: Optional[int] = None, pipeline_scope=None) -> MCPToolset:
    """A toolset bound to a fresh DatabaseManager and the knowledge base."""
    from ..config import Config
    from ..database import DatabaseManager
    from ..knowledge_base import SQLKnowledgeBase
    cfg = config or Config()
    db = DatabaseManager(cfg)
    if pipeline_scope is not None:
        import dataclasses
        db.sql_policy = dataclasses.replace(db.sql_policy, pipeline_scope=frozenset(str(p) for p in pipeline_scope))
    kb = SQLKnowledgeBase(cfg)
    providers = []
    try:
        from ..llm import get_gateway
        providers = sorted(get_gateway().providers)
    except Exception:
        providers = []
    return MCPToolset(db=db, kb=kb, config=cfg, providers=providers)


def bind_is_internal(host: str) -> bool:
    from backend.security.offline_policy import is_internal_host
    return is_internal_host(host) and host not in ("0.0.0.0", "::")


def _server_class():
    """The SDK's server class, whichever major version is installed."""
    try:
        from mcp.server import MCPServer          # mcp >= 2
        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP    # mcp 1.x
        return FastMCP
    except ImportError:
        return None


def build_mcp_server(toolset: MCPToolset, ctx: Optional[ToolContext] = None):
    """An MCP server with one tool per catalogue entry, or None when the SDK
    is not installed. Tool names use '_' in place of '.' (MCP tool names are
    identifiers); the catalogue name travels in the description."""
    server_cls = _server_class()
    if server_cls is None:
        return None
    server = server_cls("face-detector-sql-agent")
    context = ctx or ToolContext(user_id=None, role="service", request_id="mcp")

    def _make(name: str, description: str):
        def _tool(arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
            return toolset.call(name, arguments or {}, context)
        _tool.__name__ = name.replace(".", "_")
        _tool.__doc__ = description
        return _tool

    for spec in toolset.specs():
        server.add_tool(_make(spec.name, spec.description), name=spec.name.replace(".", "_"),
                        description=f"[{spec.name}] {spec.description}")
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="FACE_DETECTOR SQL agent MCP server (internal)")
    parser.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    parser.add_argument("--host", default=DEFAULT_BIND)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if args.http and not bind_is_internal(args.host):
        print(f"refusing to bind the MCP server to a non-internal address: {args.host}", file=sys.stderr)
        return 78
    server = build_mcp_server(build_toolset())
    if server is None:
        print("the 'mcp' package is not installed; the in-process toolset is still available "
              "(sql_agent.mcp.MCPToolset)", file=sys.stderr)
        return 69
    if args.http:
        try:
            server.run(transport="streamable-http", host=args.host, port=args.port,
                       streamable_http_path=MCP_PATH)
        except TypeError:                          # mcp 1.x: settings object
            server.settings.host = args.host
            server.settings.port = args.port
            server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
