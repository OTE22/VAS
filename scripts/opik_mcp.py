#!/usr/bin/env python3
"""Launch the Opik MCP server for Claude Code, aimed where the agent traces go.

`.mcp.json` runs this instead of `uvx opik-mcp` directly so that the one
place the operator configures tracing — `docker/.env`, the same file that
feeds the API container — also decides where Claude Code reads traces from.
Without it the MCP config would need the hosted API key written into a
repository file, or the operator would keep two configurations in step by
hand.

    OPIK_URL_OVERRIDE  where the SDK posts traces (default: self-hosted on
                       the workstation, as seen from the container)
    OPIK_API_KEY       hosted service only
    OPIK_WORKSPACE     hosted service only (open source has just "default")

Two facts about opik-mcp that this script encodes (opik-mcp 0.2.32):
  * it derives the REST base as COMET_URL_OVERRIDE + "/opik/api" — the hosted
    layout — unless OPIK_URL is given; the open-source stack serves "/api";
  * it posts start-up analytics to stats.comet.com unless
    OPIK_MCP_ANALYTICS_ENABLED=false.

The container reaches a workstation Opik as host.docker.internal; this
process runs ON the workstation, so that name becomes localhost.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOTENV = os.path.join(ROOT, "docker", ".env")
DEFAULT_LOCAL = "http://host.docker.internal:5173/api/"
CLOUD_HOSTS = ("comet.com", "www.comet.com")
CONTAINER_HOST_ALIASES = ("host.docker.internal", "gateway.docker.internal")


def read_dotenv(path: str) -> Dict[str, str]:
    """KEY=VALUE lines; comments, blanks and a leading BOM ignored."""
    values: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    except FileNotFoundError:
        pass
    return values


def is_cloud(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in CLOUD_HOSTS)


def mcp_environment(dotenv: Dict[str, str]) -> Dict[str, str]:
    """The OPIK_* / COMET_* variables opik-mcp needs, from the operator's
    docker/.env. Pure, so it is unit-tested without launching anything."""
    url = (dotenv.get("OPIK_URL_OVERRIDE") or DEFAULT_LOCAL).strip()
    env = {
        "OPIK_MCP_ANALYTICS_ENABLED": "false",
        "OPIK_MCP_ANALYTICS_SOURCE": "",
    }
    project = (dotenv.get("OPIK_PROJECT_NAME") or "face-detector-sql-agent").strip()
    env["OPIK_DEFAULT_PROJECT_NAME"] = project

    if is_cloud(url):
        env["COMET_URL_OVERRIDE"] = "https://www.comet.com"
        env["OPIK_URL"] = "https://www.comet.com/opik/api"
        key = (dotenv.get("OPIK_API_KEY") or "").strip()
        if key:
            env["OPIK_API_KEY"] = key
        workspace = (dotenv.get("OPIK_WORKSPACE") or "").strip()
        if workspace and workspace != "default":
            env["OPIK_WORKSPACE"] = workspace
        return env

    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    if host in CONTAINER_HOST_ALIASES:
        host = "localhost"
    port = f":{parts.port}" if parts.port else ""
    origin = f"{parts.scheme or 'http'}://{host}{port}"
    env["COMET_URL_OVERRIDE"] = origin
    env["OPIK_URL"] = origin + "/api"      # open-source layout, not /opik/api
    # No key, no workspace: the open-source backend authenticates nobody and
    # has exactly one workspace. Leaving them unset is what opik-mcp expects.
    return env


def main() -> int:
    dotenv = read_dotenv(DOTENV)
    derived = mcp_environment(dotenv)
    # The child needs the parent's PATH and friends to find uvx; only the
    # Opik variables are decided here.
    env = dict(os.environ)
    for stale in ("OPIK_API_KEY", "OPIK_WORKSPACE", "COMET_WORKSPACE", "OPIK_URL",
                  "COMET_URL_OVERRIDE"):
        env.pop(stale, None)
    env.update(derived)
    where = "HOSTED comet.com" if is_cloud(derived["OPIK_URL"]) else derived["OPIK_URL"]
    print(f"[opik-mcp launcher] traces from {where}, project "
          f"{derived['OPIK_DEFAULT_PROJECT_NAME']!r}", file=sys.stderr)
    try:
        return subprocess.run(["uvx", "opik-mcp", *sys.argv[1:]], env=env).returncode
    except FileNotFoundError:
        print("[opik-mcp launcher] `uvx` not found — install uv "
              "(https://docs.astral.sh/uv/) and make sure it is on PATH.", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
