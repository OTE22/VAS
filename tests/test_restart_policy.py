"""Every service must state how it behaves when it dies, and say it out loud.

Compose's default is `restart: "no"`. A service added without the key therefore
gets the one policy nobody wants in production, and it looks identical in the
file to a service where "no" was chosen deliberately. The failure surfaces days
later as "the API is down and nothing restarted it", after a crash at 03:00
that a single line would have recovered from.

The distinction matters in both directions. `migrate` is `"no"` ON PURPOSE: it
is a one-shot schema job, and `unless-stopped` would re-run migrations every
time the Docker daemon starts. So this cannot simply assert "everything is
unless-stopped" - it asserts each service carries the policy its ROLE requires.

Note `unless-stopped` only restores containers at boot if Docker itself starts
at boot; verified separately with `systemctl is-enabled docker`.

Run:  python -m pytest tests/test_restart_policy.py -v
"""

import io
import os

import pytest
import yaml

from tests._repo_scan import find_repo_root

REPO = find_repo_root()
STACKS = ["docker/docker-compose.prod.yml", "docker/docker-compose.cpu.yml"]

# Services that run to completion and exit. Restarting them repeats the work:
# for migrate that means re-applying schema revisions on every daemon start.
ONE_SHOT = {"migrate"}


def _services(stack):
    with io.open(os.path.join(REPO, stack), encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("services", {}) or {}


@pytest.mark.parametrize("stack", STACKS)
def test_every_service_declares_a_restart_policy(stack):
    """Absent means "no" - silently, and indistinguishably from a choice."""
    silent = sorted(name for name, service in _services(stack).items()
                    if "restart" not in service)
    assert not silent, (
        f"{stack}: these services declare no restart policy, so Compose "
        f"applies 'no' and a crash is permanent: {silent}")


@pytest.mark.parametrize("stack", STACKS)
def test_long_running_services_restart_unless_stopped(stack):
    """`unless-stopped`, not `always`: an operator who deliberately stops a
    container must find it still stopped after a daemon restart."""
    wrong = []
    for name, service in _services(stack).items():
        if name in ONE_SHOT:
            continue
        policy = str(service.get("restart", "")).strip('"')
        if policy != "unless-stopped":
            wrong.append(f"{name}={policy or '<absent>'}")
    assert not wrong, (
        f"{stack}: long-running services must be unless-stopped: {wrong}")


@pytest.mark.parametrize("stack", STACKS)
def test_one_shot_jobs_do_not_restart(stack):
    """The inverse guard: making migrate `unless-stopped` to be consistent
    would re-run migrations on every daemon start."""
    wrong = []
    for name in ONE_SHOT & set(_services(stack)):
        policy = str(_services(stack)[name].get("restart", "")).strip('"')
        if policy != "no":
            wrong.append(f"{name}={policy or '<absent>'}")
    assert not wrong, (
        f"{stack}: one-shot jobs must be restart:'no' or they repeat their "
        f"work on every daemon start: {wrong}")
