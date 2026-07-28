"""Script-order contract: window.Actions must exist before page scripts run.

Observed live: `Uncaught ReferenceError: Actions is not defined` at
home.js:245. actions.js (which defines window.Actions) was loaded with
`defer`, so it executed AFTER parsing — i.e. after every non-deferred page
script had already run its top-level `Actions.register(...)`. Six pages had
the mismatch; on each, every data-action click handler was dead and any code
after the crashed registration never executed.

The contract: actions.js is loaded WITHOUT defer everywhere it appears.
It only registers document-level delegated listeners and sets window.Actions,
so synchronous execution before DOM parse is safe by construction.
"""

import glob
import os
import re

import pytest

FRONTEND = "/app/frontend"


def _pages():
    pages = (glob.glob(os.path.join(FRONTEND, "*.html"))
             + glob.glob(os.path.join(FRONTEND, "admin", "*.html"))
             + glob.glob(os.path.join(FRONTEND, "components", "*.html")))
    assert pages, "no frontend pages found"
    return pages


def test_actions_js_is_never_deferred():
    offenders = []
    for page in _pages():
        with open(page, encoding="utf-8") as f:
            src = f.read()
        for tag in re.findall(r'<script[^>]*actions\.js[^>]*>', src):
            if "defer" in tag:
                offenders.append(page)
    assert not offenders, (
        f"actions.js loaded with defer in {offenders} — window.Actions will "
        f"not exist when non-deferred page scripts call Actions.register()"
    )


def test_every_page_with_registering_script_loads_actions_js_first():
    """A page whose script calls Actions.register must include actions.js,
    and the actions.js tag must appear BEFORE that script tag."""
    registering = set()
    for js in glob.glob(os.path.join(FRONTEND, "js", "*.js")):
        with open(js, encoding="utf-8") as f:
            if "Actions.register" in f.read():
                registering.add(os.path.basename(js))

    assert registering, "no registering scripts found — did the pattern change?"

    problems = []
    for page in _pages():
        with open(page, encoding="utf-8") as f:
            src = f.read()
        actions_at = src.find("js/actions.js")
        for name in registering:
            page_script_at = src.find(f"js/{name}")
            if page_script_at == -1:
                continue
            if actions_at == -1:
                problems.append(f"{page} loads {name} but never loads actions.js")
            elif actions_at > page_script_at:
                problems.append(f"{page} loads {name} before actions.js")
    assert not problems, problems


def test_home_pipelines_loader_survives_missing_markup():
    """home.html has never contained #pipelines-section; the loader must not
    dereference null (this logged an error on every home page load)."""
    with open(os.path.join(FRONTEND, "js", "home.js"), encoding="utf-8") as f:
        src = f.read()
    loader = src.split("async function loadPipelines", 1)[1].split("\nfunction ", 1)[0]
    # Intent, not identifiers: the loader must bail out before touching the DOM
    # when its markup is absent. (Asserting the exact variable names made this
    # test fail on a pure rename during the dashboard redesign.)
    guard = re.search(r"if \(!\w+ \|\| !\w+\) return;", loader)
    assert guard, (
        "loadPipelines has no early return for missing markup — it would "
        "dereference null once the pipelines response arrives"
    )
    # ...and the guard must come before any DOM mutation.
    first_write = min(
        (loader.index(tok) for tok in ("replaceChildren", "appendChild", ".style.")
         if tok in loader),
        default=len(loader),
    )
    assert guard.start() < first_write, "the null guard runs after a DOM write"

    # Server data must not be interpolated into pipeline markup. Checked on
    # NON-COMMENT lines only: the loader documents why it avoids innerHTML,
    # and a naive substring match reads that explanation as the violation.
    loader_code = "\n".join(
        line for line in loader.splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )
    assert "innerHTML" not in loader_code, (
        "loadPipelines builds HTML from server-supplied pipeline data"
    )
