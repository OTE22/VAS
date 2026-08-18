"""The in-app tutorial must match the running build, and every section must be
reachable.

    docker exec face_recognition_api python -m pytest tests/test_admin_tutorial.py -v

The tutorial is the one document that is *supposed* to track the code, so a
section the UI cannot open is worse than a missing document: the index tells
administrators to go read it and the page silently has no button for it.

That is exactly what had happened — five of thirteen sections had no nav button
(including "Platform Hardening: What Changed") and two buttons pointed at ids no
section produced, because the nav was hardcoded in tutorial.html while the
sections come from the API.
"""

import json
import pathlib
import re
import urllib.request

import pytest

BASE = "http://localhost:8000"
REPO = pathlib.Path("/app")
TUTORIAL_JS = REPO / "frontend" / "js" / "admin-tutorial.js"
TUTORIAL_HTML = REPO / "frontend" / "admin" / "tutorial.html"


@pytest.fixture(scope="module")
def token():
    data = json.dumps({"username": "admin", "password": "admin123"}).encode()
    request = urllib.request.Request(
        BASE + "/api/auth/login", data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["access_token"]


@pytest.fixture(scope="module")
def tutorial(token):
    request = urllib.request.Request(BASE + "/api/admin/tutorial")
    request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())


def _section_id(title):
    """Mirrors getSectionId() in admin-tutorial.js."""
    short = {
        "API Authentication": "authentication",
        "Understanding Unknown Faces": "understanding",
        "Promoting Unknown to Known": "promote",
        "Merging Identities": "merge",
        "Quick Search": "search",
        "Advanced Search": "advanced-search",
        "System Settings Management": "settings",
        "System Workflow": "workflow",
        "Advanced SNA Features": "advanced-sna",
    }
    if title in short:
        return short[title]
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", title.lower()))


def test_the_tutorial_endpoint_answers(tutorial):
    assert tutorial["sections"], "the tutorial returned no sections"
    assert tutorial["quick_start"]
    assert tutorial["common_tasks"]


def test_every_section_has_content_and_a_description(tutorial):
    thin = [s["title"] for s in tutorial["sections"]
            if len(s.get("content", "")) < 200 or not s.get("description")]
    assert not thin, f"sections with no real content or description: {thin}"


def test_the_navigation_is_built_from_the_api_not_hardcoded(tutorial):
    """The HTML must not carry a hand-maintained button per section — that is
    the arrangement that drifted. Only the pre-load placeholder may remain."""
    html = TUTORIAL_HTML.read_text(encoding="utf-8")
    # Match the container by class, allowing any other attributes — it carries
    # role="tablist" and aria-label now, and an exact-tag regex would fail on
    # an accessibility improvement rather than on the thing being guarded.
    nav = re.search(r'<div class="tutorial-nav"[^>]*>(.*?)</div>', html, re.S)
    assert nav, "the .tutorial-nav container is gone from tutorial.html"
    hardcoded = re.findall(r'data-section="([^"]+)"', nav.group(1))
    assert hardcoded == ["quick-start"], (
        f"tutorial.html hardcodes nav buttons {hardcoded}. The nav must be "
        f"rendered from the API response by renderNav(), or it will drift out "
        f"of sync with the sections again.")

    js = TUTORIAL_JS.read_text(encoding="utf-8")
    assert "function renderNav" in js, "renderNav() is missing from admin-tutorial.js"
    assert re.search(r"renderNav\(\s*\)\s*;", js), "renderNav() is never called"


def test_every_section_id_is_a_usable_dom_id(tutorial):
    """`document.getElementById('section-' + id)` must be able to find it, and
    the id must survive being written into a selector. A ':' from a title like
    "Platform Hardening: What Changed" broke both."""
    bad = []
    for section in tutorial["sections"]:
        section_id = _section_id(section["title"])
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", section_id):
            bad.append(f"{section['title']!r} -> {section_id!r}")
    assert not bad, f"section ids are not safe slugs: {bad}"


def test_no_two_sections_collide_on_the_same_id(tutorial):
    seen = {}
    collisions = []
    for section in tutorial["sections"]:
        section_id = _section_id(section["title"])
        if section_id in seen:
            collisions.append(f"{section_id}: {seen[section_id]!r} and {section['title']!r}")
        seen[section_id] = section["title"]
    assert not collisions, (
        f"two sections share a DOM id, so one is unreachable: {collisions}")


def test_the_current_operating_guidance_is_present(tutorial):
    """The audit's findings live in the tutorial as well as in Docs/ — this is
    the copy an administrator actually sees."""
    titles = [s["title"] for s in tutorial["sections"]]
    assert any("Operating This System" in t for t in titles), (
        f"the operating-guidance section is missing; sections are {titles}")

    section = next(s for s in tutorial["sections"] if "Operating This System" in s["title"])
    content = section["content"]
    for expected in ("restart nginx",            # the stale-upstream 502
                     "force-recreate",           # env changes need a recreate
                     "effective_value",          # DB overrides the environment
                     "/app/alembic",             # alembic working directory
                     "down -v",                  # destructive command warning
                     "75_API_REFERENCE.md"):     # where the API reference is
        assert expected in content, (
            f"the operating section no longer mentions {expected!r}")


def test_the_tutorial_does_not_promise_docs_in_production(tutorial):
    """/docs is disabled in production; the tutorial must not say otherwise."""
    for section in tutorial["sections"]:
        for line in section["content"].splitlines():
            if "/docs" not in line:
                continue
            if re.search(r"404|disabled|not available|development", line, re.I):
                continue
            assert "available" not in line.lower(), (
                f"{section['title']!r} claims /docs is available without noting "
                f"that production disables it: {line.strip()!r}")
