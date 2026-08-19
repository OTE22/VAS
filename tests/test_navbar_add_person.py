"""ADD PERSON works on every page that shows it.

The navbar renders

    <a href="#" class="dropdown-item" data-page="add-person"
       data-action="openUploadModal">

but the handler ships with the upload-modal component, and only 8 of the 21
pages that inject the navbar loaded that component. On the other 13 —
intelligence, settings, logs, pipelines, watchlists, audit, search-history and
the rest — the menu entry rendered, the click found no registered action, and
the bare `#` href did the only thing left: put a `#` on the URL. Pressing ADD
PERSON appeared to do nothing.

Fixed where the dependency belongs: navbar-loader.js renders the control, so
it ensures the component is present. Behavioural proof (dropdown opened, item
clicked, modal observed opening on six pages) lives in
scripts/dev/add_person_probe.js, which drives a real browser; these are the
static guards that keep the wiring from being unpicked.
"""

import os
import re

import pytest

FRONTEND = "/app/frontend"


def _pages():
    """Every shipped page that injects the shared navbar."""
    found = []
    for root in (FRONTEND, os.path.join(FRONTEND, "admin")):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".html"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                if "navbar-loader" in handle.read():
                    found.append(path)
    return found


def test_the_navbar_still_renders_the_add_person_item():
    with open(f"{FRONTEND}/components/admin-navbar.html", encoding="utf-8") as handle:
        navbar = handle.read()
    assert 'data-page="add-person"' in navbar
    assert 'data-action="openUploadModal"' in navbar, (
        "the item lost its action and would be a dead `#` link again")


def test_the_navbar_loader_supplies_the_upload_modal_itself():
    """The control's owner owns its dependency — not 21 separate script tags."""
    with open(f"{FRONTEND}/js/navbar-loader.js", encoding="utf-8") as handle:
        source = handle.read()
    assert "function ensureUploadModal" in source, (
        "navbar-loader no longer guarantees the upload modal; every page that "
        "does not carry the script tag gets a dead ADD PERSON link")
    assert "ensureUploadModal()" in source, "the helper is defined but never called"
    assert "upload-modal-loader.js" in source


def test_pages_with_the_navbar_are_covered():
    """Whether by their own tag or by the loader — but covered."""
    pages = _pages()
    assert len(pages) > 10, f"expected the navbar on many pages, found {len(pages)}"
    with open(f"{FRONTEND}/js/navbar-loader.js", encoding="utf-8") as handle:
        loader_covers = "ensureUploadModal" in handle.read()
    assert loader_covers, (
        "with no fallback in the loader, these pages would need their own "
        f"tag: {[p for p in pages if 'upload-modal-loader' not in open(p, encoding='utf-8').read()]}")


def test_loading_the_component_twice_cannot_duplicate_the_modal():
    """Eight pages carry the tag AND get the loader's guarantee. Two
    #uploadModal nodes would make every getElementById find a stale one."""
    with open(f"{FRONTEND}/js/upload-modal-loader.js", encoding="utf-8") as handle:
        loader = handle.read()
    body = loader.split("async function loadUploadModal()")[1]
    guard = body.split("try")[0]
    assert "getElementById('uploadModal')" in guard or \
           'getElementById("uploadModal")' in guard, (
        "the loader does not check whether the modal is already in the DOM")

    with open(f"{FRONTEND}/js/navbar-loader.js", encoding="utf-8") as handle:
        navbar = handle.read()
    ensure = navbar.split("function ensureUploadModal()")[1].split("\n    }")[0]
    assert "upload-modal-loader.js" in ensure and "return" in ensure, (
        "ensureUploadModal must not re-inject the loader when a page already "
        "has the script tag")


@pytest.mark.parametrize("page", _pages())
def test_every_navbar_page_pins_the_same_loader_version(page):
    """A stale cached navbar-loader is a page where ADD PERSON is still dead."""
    with open(page, encoding="utf-8") as handle:
        html = handle.read()
    tags = re.findall(r'navbar-loader\.js\?v=([\w.-]+)', html)
    assert tags, f"{os.path.basename(page)} loads navbar-loader without a version pin"
    assert set(tags) == {"nav-7"}, (
        f"{os.path.basename(page)} pins {set(tags)}; the fix ships in nav-7")
