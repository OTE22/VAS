"""One layer hierarchy, and a guard against escalating out of it again.

Add Person rendered UNDER page elements and navbar dropdowns rendered BEHIND
page content because the layer tokens in admin.css (:root) were bypassed by 38
raw declarations. `.modal` alone carried four different values across
stylesheets — 2000 in admin.css and admin-search.css, 10000 in
admin-search-history.css and upload-modal.css — while dashboard furniture sat
at 14997-15000 and a face-alert dialog carried an inline 99999. Whoever wrote
each one had no way to know what the others had chosen.

The rule now: anything entering the global layering range names a token. Small
local values (stacking two children of one component) stay free.

Runtime proof that the ORDER is right lives in the browser probes
(scripts/dev/layering_probe.js, dropdown_scroll_probe.js) — a CSS rule being
present says nothing about what the pointer actually hits. These tests only
keep the architecture from being unpicked.
"""

import os
import re

import pytest

FRONTEND = "/app/frontend"
CSS_DIR = os.path.join(FRONTEND, "css")

# Below the lowest global tier, a z-index only orders siblings inside one
# component and is nobody else's business (a sticky table header at 100 cannot
# reach the navbar). At or above it, the value competes with navigation,
# modals and toasts, so it must come from the shared scale. Derived from the
# tokens rather than guessed, so the guard follows the scale if it moves.
def _global_layer_floor():
    values = _token_values()
    return min(values.get("--z-page-popover", 900), values.get("--z-nav", 1000))

Z_DECL = re.compile(r"z-index\s*:\s*([^;}\n]+)", re.IGNORECASE)

# Values allowed to stay literal, with the reason. Anything else at or above
# the floor must use var(--z-*).
EXEMPT = {
    # Skip links must outrank page chrome for keyboard users and are rendered
    # off-screen until focused. signin.html does not load admin.css, so a token
    # would not resolve there.
    "signin.css": {"9999"},
}

REQUIRED_TOKENS = [
    "--z-content-overlay",
    "--z-page-popover",
    "--z-nav",
    "--z-dropdown",
    "--z-modal-base",
    "--z-modal-step",
    "--z-modal-popover",
    "--z-toast",
]


def _admin_css():
    with open(os.path.join(CSS_DIR, "admin.css"), encoding="utf-8") as handle:
        return handle.read()


def _token_values():
    """The declared scale, as {name: int}."""
    source = _admin_css()
    root = source.split(":root", 1)[1].split("}", 1)[0]
    found = {}
    for name in REQUIRED_TOKENS:
        match = re.search(re.escape(name) + r"\s*:\s*(\d+)", root)
        if match:
            found[name] = int(match.group(1))
    return found


def test_every_layer_token_is_declared():
    tokens = _token_values()
    missing = [name for name in REQUIRED_TOKENS if name not in tokens]
    assert not missing, f"layer tokens missing from admin.css :root: {missing}"


def test_the_hierarchy_is_ordered_as_documented():
    """The order IS the contract; the numbers are just how it is expressed."""
    t = _token_values()
    assert t["--z-content-overlay"] < t["--z-page-popover"] < t["--z-nav"], (
        "content overlays and page popovers must sit below the navbar, or a "
        "navbar dropdown cannot paint above page content — the navbar is a "
        "stacking context and its menu is trapped inside it")
    assert t["--z-nav"] < t["--z-dropdown"], "the navbar menu sits above the navbar"
    assert t["--z-dropdown"] < t["--z-modal-base"], "modals sit above navigation"
    assert t["--z-modal-base"] < t["--z-modal-popover"] < t["--z-toast"], (
        "a modal-owned popover sits above its modal, and toasts above both")


@pytest.mark.parametrize("css", sorted(
    f for f in os.listdir(CSS_DIR) if f.endswith(".css")
))
def test_no_raw_z_index_in_the_global_layering_range(css):
    """Catches a future `z-index: 50000` — and would have caught every one of
    the 9999 / 10000 / 14997-15000 / 99999 values this replaced."""
    path = os.path.join(CSS_DIR, css)
    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    offenders = []
    for match in Z_DECL.finditer(source):
        raw = match.group(1).strip()
        if "var(" in raw:
            continue                      # already on the scale
        bare = re.fullmatch(r"-?\d+", raw)
        if not bare:
            continue                      # calc(), inherit, auto, initial
        value = int(raw)
        if value < _global_layer_floor():
            continue                      # local ordering, not our business
        if raw in EXEMPT.get(css, set()):
            continue
        line = source[:match.start()].count("\n") + 1
        offenders.append(f"{css}:{line} z-index:{raw}")

    assert not offenders, (
        "raw z-index in the global layering range — use var(--z-*) from "
        f"admin.css :root instead: {offenders}")


def test_no_inline_z_index_in_component_or_page_markup():
    """An inline z-index beats every stylesheet, so it beats the hierarchy too.
    #face-detection-alert-modal shipped `style="z-index: 99999"` in the shared
    upload-modal component, which put it above the toast layer on every page
    that injected it."""
    offenders = []
    for folder in (FRONTEND, os.path.join(FRONTEND, "admin"),
                   os.path.join(FRONTEND, "components")):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".html"):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    for match in re.finditer(r"style=\"[^\"]*z-index:\s*(\d+)", line):
                        if int(match.group(1)) < _global_layer_floor():
                            continue
                        if "skip-to-main" in line:
                            # Keyboard skip link: an inline style is the
                            # standard pattern for it, it is positioned
                            # off-screen until focused, and at 999 it sits
                            # deliberately just below the navbar tier.
                            continue
                        offenders.append(
                            f"{os.path.relpath(path, FRONTEND)}:{number} "
                            f"z-index:{match.group(1)}")
    assert not offenders, f"inline z-index overriding the layer scale: {offenders}"


def test_modal_stack_reaches_every_page_that_can_open_a_modal():
    """Add Person is injected into every page carrying the navbar. Without the
    stack, upload-modal.js used to fall back to `display = 'flex'`: no managed
    layering, no background suppression, no scroll lock. That fallback is gone,
    so a page missing the script would now throw instead of degrading."""
    missing = []
    for folder in (FRONTEND, os.path.join(FRONTEND, "admin")):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".html"):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as handle:
                html = handle.read()
            if "navbar-loader.js" in html and "modal-stack.js" not in html:
                missing.append(os.path.relpath(path, FRONTEND))
    assert not missing, f"pages render the navbar but never load modal-stack.js: {missing}"


def test_upload_modal_has_exactly_one_open_path():
    """Two paths meant two behaviours; the fallback was the broken one."""
    with open(os.path.join(FRONTEND, "js", "upload-modal.js"), encoding="utf-8") as handle:
        raw = handle.read()
    # Statements only. The comment above the call NAMES the removed fallback to
    # explain what changed, and matching raw text would flag that as the bug
    # still being present.
    source = "\n".join(re.sub(r"//.*$", "", line) for line in raw.splitlines())
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    assert "modal.style.display = 'flex'" not in source, (
        "the unmanaged fallback is back — it bypasses layering, suppression "
        "and the scroll lock")
    assert "window.ModalStack.open(modal" in source


def test_the_scroll_lock_captures_and_restores_real_state():
    """Not `overflow='hidden'` / `overflow=''`: the page's own inline styles and
    scroll position have to come back exactly."""
    with open(os.path.join(FRONTEND, "js", "modal-stack.js"), encoding="utf-8") as handle:
        source = handle.read()
    # Split on the name only: lockPage takes the modal being opened, so
    # matching "function lockPage()" pinned a signature rather than behaviour.
    assert "function lockPage(" in source, "lockPage is gone"
    lock = source.split("function lockPage(", 1)[1].split("function unlockPage(", 1)[0]
    for prop in ("overflow", "position", "top", "left", "right", "width", "paddingRight"):
        assert prop in lock, f"lockPage does not capture body.style.{prop}"
    assert "scrollY" in lock, "lockPage does not capture the scroll position"
    assert "if (pageLock) { return; }" in lock, (
        "a nested open would re-capture the already-locked state and restore "
        "the wrong values on the final close")


def test_the_scroll_lock_never_freezes_the_dialog_it_is_opening():
    """The lock freezes background scrollers by setting overflow:hidden on
    them. It collected the dialog too — lockPage runs after the dialog is
    displayed, so a tall dialog was already scrolling and got frozen, leaving
    everything past the fold unreachable with the page locked behind it."""
    with open(os.path.join(FRONTEND, "js", "modal-stack.js"), encoding="utf-8") as handle:
        source = handle.read()

    assert "function verticalScrollers(exclude)" in source, (
        "verticalScrollers takes no exclusion, so it can collect the dialog's "
        "own scroller and freeze it")
    scanner = source.split("function verticalScrollers(", 1)[1].split("function lockPage(", 1)[0]
    assert "exclude.contains(el)" in scanner, (
        "the scanner does not skip the dialog's subtree")
    assert "lockPage(el)" in source, (
        "open() does not tell lockPage which dialog to leave alone")


def test_suppression_restores_prior_accessibility_state():
    """An element already aria-hidden before a modal opened must stay that way
    after it closes."""
    with open(os.path.join(FRONTEND, "js", "modal-stack.js"), encoding="utf-8") as handle:
        source = handle.read()
    assert "priorSuppression" in source, "no prior-state capture for suppression"
    unsuppress = source.split("function unsuppress(el)", 1)[1].split("\n    }", 1)[0]
    assert "prior.ariaHidden" in unsuppress and "prior.inert" in unsuppress, (
        "unsuppress() clears attributes instead of restoring what was there")
