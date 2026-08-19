"""The settings writer's CSRF guard, exercised over COOKIES.

`require_settings_csrf` was added to PUT /api/settings/{key} and the frontend
side of it was not. Every "Save Changes" on the settings page answered

    403 CSRF check failed: X-Requested-With header required

and it stayed that way because the guard exempts bearer-token callers — which
is every test in this suite. `test_the_settings_writer_requires_csrf` passed
throughout: it inspects the route signature for a `_csrf` parameter, so it
proves the dependency is wired up and nothing about whether a browser can
still save.

These tests drive the path a browser actually uses: log in as a browser client
(HttpOnly cookie, no token in the body) and PUT with and without the header.
"""

import http.cookiejar
import json
import urllib.error
import urllib.request

import pytest

BASE = "http://localhost:8000"


def _opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def _call(opener, method, path, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except Exception:                                      # noqa: BLE001
            payload = {}
        return exc.code, payload


@pytest.fixture(scope="module")
def browser_session():
    """A session authenticated the way the page is: cookie only."""
    opener, jar = _opener()
    status, body = _call(opener, "POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"},
                         headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, f"browser login failed: {body}"
    assert not body.get("access_token"), (
        "a browser client must receive no token in the body — the credential "
        "belongs in the HttpOnly cookie")
    assert list(jar), "login set no cookie, so there is no browser session to test"
    return opener


@pytest.fixture(scope="module")
def probe(browser_session):
    """A real setting, discovered rather than hard-coded, that this test can
    write back to the value it already has: applied immediately, so no restart
    is provoked, and a no-op change.
    """
    status, body = _call(browser_session, "GET", "/api/settings",
                         headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, body
    candidates = [s for s in body["all_settings"]
                  if s.get("apply_mode") == "immediate"
                  and not s.get("requires_restart")
                  and not s.get("requires_worker_restart")
                  and not s.get("requires_index_rebuild")
                  and s.get("value") is not None]
    assert candidates, "no immediately-applied setting to probe with"
    chosen = sorted(candidates, key=lambda s: s["key"])[0]
    return chosen["key"], str(chosen["value"])


def test_a_cookie_write_without_the_header_is_refused(browser_session, probe):
    key, value = probe
    status, body = _call(browser_session, "PUT", f"/api/settings/{key}",
                         {"value": value, "change_reason": "pytest csrf probe"})
    assert status == 403, (status, body)
    assert "X-Requested-With" in str(body.get("detail", "")), body


def test_a_cookie_write_with_the_header_succeeds(browser_session, probe):
    """The regression that matters: this is exactly what the page now sends."""
    key, value = probe
    status, body = _call(browser_session, "PUT", f"/api/settings/{key}",
                         {"value": value, "change_reason": "pytest csrf probe"},
                         headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, (
        f"a browser cannot save {key}: {status} {body}. This is the reported "
        f"'CSRF check failed' failure.")


def test_the_settings_page_sends_the_header_on_every_write():
    """Supplementary. The behavioural proof is above; this pins the mechanism
    to the single place the page reaches the API, so a new call cannot be
    added that forgets it."""
    with open("/app/frontend/js/admin-settings.js", encoding="utf-8") as handle:
        source = handle.read()
    helper = source.split("async function apiFetch")[1].split("\nfunction ")[0]
    assert "'X-Requested-With': 'XMLHttpRequest'" in helper, (
        "apiFetch does not send the header the settings writer requires")
    # Every write on this page goes through apiFetch rather than bare fetch().
    body = source.split("async function apiFetch")[1]
    assert "fetch(" not in body.replace("await fetch(", ""), (
        "a bare fetch() bypasses apiFetch and will miss the CSRF header")
