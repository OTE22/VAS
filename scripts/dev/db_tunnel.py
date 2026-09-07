#!/usr/bin/env python3
"""Expose the production database to pgAdmin 4 — temporarily, and safely.

    python3 scripts/dev/db_tunnel.py --start
    python3 scripts/dev/db_tunnel.py --status
    python3 scripts/dev/db_tunnel.py --stop

WHY THIS EXISTS
---------------
Postgres deliberately publishes NO host port. It sits on the `data` docker
network only, so nothing outside the stack can reach it even if nginx were
compromised. That is a security property worth keeping, so this tool does NOT
change compose, does not publish a port, and does not survive a reboot.

What it does instead: the host can already reach the postgres container over
the docker bridge (172.21.x.x:5432). This starts a small TCP forwarder that
listens on 127.0.0.1:5432 and relays to the container. Stop it and the exposure
is gone.

CONNECTING FROM ANOTHER MACHINE
-------------------------------
Do NOT bind this to the LAN. Tunnel over SSH from the client instead — the
database stays unreachable from the network, and the traffic is encrypted:

    ssh -L 5432:127.0.0.1:5432 itdirect-ai@192.168.1.111

Then point pgAdmin at localhost:5432 on your own machine.

CREDENTIALS
-----------
This script never reads, stores or prints a password. pgAdmin asks you for one;
retrieve it yourself from the file compose reads:

    sudo grep '^FR_READONLY_PASSWORD=' docker/.env      # SELECT only  (safest)
    sudo grep '^FR_APP_PASSWORD='      docker/.env      # read + write

Prefer `fr_readonly`. It cannot modify anything, which is what you want for
browsing a production database.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CONTAINER = "face_detector_prod-postgres-1"
DB_PORT = 5432
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 5432
# tempfile.gettempdir(), not os.environ: config.py is this repo's only
# configuration interface, and tests/test_config_single_source.py enforces
# that no module reads the environment directly. gettempdir() already
# honours TMPDIR and needs no environment read of our own.
STATE = Path(tempfile.gettempdir()) / "vas-db-tunnel.json"


# ---------------------------------------------------------------------------
# Locating the database
# ---------------------------------------------------------------------------

def container_ip() -> str:
    """The container's current address on the docker network.

    Resolved on every start, never cached: docker reassigns these when a
    container is recreated, so a remembered address silently forwards to
    whatever occupies it next.
    """
    try:
        out = subprocess.run(
            ["docker", "inspect", CONTAINER, "--format",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"],
            capture_output=True, text=True, timeout=15, check=True).stdout
    except FileNotFoundError:
        sys.exit("docker command not found")
    except subprocess.CalledProcessError:
        sys.exit(f"{CONTAINER} not found — is the stack running? (sudo ./deploy.sh status)")
    except subprocess.TimeoutExpired:
        sys.exit("docker inspect timed out")

    addresses = [a for a in out.split() if a]
    if not addresses:
        sys.exit(f"{CONTAINER} has no IP address — is it running?")
    return addresses[0]


# ---------------------------------------------------------------------------
# The forwarder
# ---------------------------------------------------------------------------

async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _serve(bind: str, port: int, target: str) -> None:
    async def handle(client_reader, client_writer):
        try:
            db_reader, db_writer = await asyncio.open_connection(target, DB_PORT)
        except OSError as e:
            print(f"  cannot reach {target}:{DB_PORT}: {e}", flush=True)
            client_writer.close()
            return
        # Both directions concurrently; when either side closes, tear down.
        await asyncio.gather(
            _pipe(client_reader, db_writer),
            _pipe(db_reader, client_writer),
            return_exceptions=True,
        )

    server = await asyncio.start_server(handle, bind, port)
    # State is written HERE, after the bind succeeds - never before. Writing it
    # earlier meant a tunnel that failed to bind still looked "running", and a
    # detached child that read the file saw its own pid and refused to start.
    STATE.write_text(json.dumps(
        {"pid": os.getpid(), "bind": bind, "port": port, "target": target}))
    print(f"  forwarding {bind}:{port} -> {target}:{DB_PORT}", flush=True)
    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
# start / stop / status
# ---------------------------------------------------------------------------

def _find_orphan() -> dict | None:
    """Locate a running forwarder when the state file is gone.

    Without this the tunnel becomes unmanageable if the state file is lost or
    removed while the process lives: --stop cannot find it, and --start dies
    with "address already in use" and no way to recover except hunting the pid
    by hand. Matching on our own script path is precise enough, and it never
    matches the caller because a script's body is not in any argv.
    """
    try:
        out = subprocess.run(["pgrep", "-af", os.path.basename(__file__)],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    me = os.getpid()
    for line in out.splitlines():
        pid_text, _, cmd = line.partition(" ")
        if "--foreground" not in cmd:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == me:
            continue
        bind, port = DEFAULT_BIND, DEFAULT_PORT
        parts = cmd.split()
        for flag, setter in (("--bind", "bind"), ("--port", "port")):
            if flag in parts:
                value = parts[parts.index(flag) + 1]
                if setter == "bind":
                    bind = value
                else:
                    port = int(value)
        return {"pid": pid, "bind": bind, "port": port, "target": "(recovered)"}
    return None


def _read_state() -> dict | None:
    if not STATE.exists():
        return _find_orphan()
    try:
        state = json.loads(STATE.read_text())
    except (ValueError, OSError):
        return None
    # A stale file outlives the process it describes; verify before trusting it.
    try:
        os.kill(int(state["pid"]), 0)
    except (OSError, KeyError, ValueError):
        with contextlib.suppress(OSError):
            STATE.unlink()
        return None
    return state


def cmd_status() -> int:
    state = _read_state()
    if not state:
        print("  tunnel: NOT running")
        return 1
    print(f"  tunnel: running (pid {state['pid']})")
    print(f"    listening : {state['bind']}:{state['port']}")
    print(f"    target    : {state['target']}:{DB_PORT}")
    return 0


def cmd_stop() -> int:
    state = _read_state()
    if not state:
        print("  tunnel: not running — nothing to stop")
        return 0
    pid = int(state["pid"])
    os.kill(pid, signal.SIGTERM)
    with contextlib.suppress(OSError):
        STATE.unlink()
    print(f"  stopped (pid {pid}); the database is unreachable from outside again")
    return 0


def cmd_start(bind: str, port: int, foreground: bool) -> int:
    if (state := _read_state()):
        print(f"  already running (pid {state['pid']}) on "
              f"{state['bind']}:{state['port']} — use --stop first")
        return 1

    target = container_ip()

    if bind not in ("127.0.0.1", "localhost", "::1"):
        print(f"  WARNING: binding {bind} exposes the production database to the")
        print( "           network in plaintext. Prefer the default loopback bind")
        print( "           and an SSH tunnel from the client:")
        print(f"             ssh -L {port}:127.0.0.1:{port} $USER@<this-host>")

    if not foreground:
        # Detach so the shell stays usable. Errors go to a log rather than
        # /dev/null: a tunnel that dies silently is impossible to diagnose.
        log = STATE.with_suffix(".log")
        with open(log, "w") as handle:
            child = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--start",
                 "--bind", bind, "--port", str(port), "--foreground"],
                stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)

        # Wait for the child to actually bind. It writes the state file only on
        # success, so this distinguishes "listening" from "started and died".
        for _ in range(50):
            if child.poll() is not None:
                sys.stderr.write(log.read_text() or "")
                print(f"  tunnel failed to start (see {log})")
                return 1
            if _read_state():
                _print_connection(bind, port, target)
                return 0
            time.sleep(0.1)
        child.terminate()
        print(f"  tunnel did not come up within 5s (see {log})")
        return 1

    try:
        asyncio.run(_serve(bind, port, target))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"  cannot listen on {bind}:{port}: {e}", flush=True)
        return 1
    finally:
        with contextlib.suppress(OSError):
            STATE.unlink()
    return 0


def _print_connection(bind: str, port: int, target: str) -> None:
    print()
    print("  pgAdmin 4 — Register > Server > Connection")
    print("  ----------------------------------------------")
    print(f"    Host name/address : {bind}")
    print(f"    Port              : {port}")
    print( "    Maintenance DB    : face_recognition")
    print( "    Username          : fr_readonly     (SELECT only — recommended)")
    print( "    Password          : see below; this script never handles it")
    print()
    print("    sudo grep '^FR_READONLY_PASSWORD=' docker/.env")
    print()
    print(f"  forwarding to the postgres container at {target}:{DB_PORT}")
    print( "  stop it when you are done:  python3 scripts/dev/db_tunnel.py --stop")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Temporarily expose the production database to pgAdmin 4.",
        epilog="The database publishes no host port by design; this forwards to "
               "it on loopback and stops when you say so.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true", help="start the tunnel")
    action.add_argument("--stop", action="store_true", help="stop it")
    action.add_argument("--status", action="store_true", help="is it running?")
    parser.add_argument("--bind", default=DEFAULT_BIND,
                        help=f"address to listen on (default {DEFAULT_BIND}; "
                             "anything else exposes the database to the network)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"local port (default {DEFAULT_PORT})")
    parser.add_argument("--foreground", action="store_true",
                        help="run in this terminal instead of detaching")
    args = parser.parse_args()

    if args.status:
        return cmd_status()
    if args.stop:
        return cmd_stop()
    return cmd_start(args.bind, args.port, args.foreground)


if __name__ == "__main__":
    sys.exit(main())
