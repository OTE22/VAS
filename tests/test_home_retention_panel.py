"""The home page's read-only retention panel stays wired end to end.

Four things must agree or the panel shows a dash where a number belongs:
the setting names `_retention_policy()` reads must exist in config.py, every
`retention.<key>` path home.js renders must be a key that function returns,
every tile id home.js writes must exist in home.html, and the API container
must receive BACKUP_RETENTION_DAYS from compose — otherwise the panel reports
config.py's default while the backup container prunes with a different value.

Run:  python -m pytest tests/test_home_retention_panel.py -v
"""

import io
import os
import re

from tests._repo_scan import find_repo_root

REPO = find_repo_root()


def _read(rel):
    with io.open(os.path.join(REPO, rel), encoding="utf-8") as handle:
        return handle.read()


def _policy_body():
    src = _read("backend/routes/stats.py")
    match = re.search(r"def _retention_policy\(\).*?\n    \}\n", src, re.S)
    assert match, "_retention_policy() is gone from backend/routes/stats.py"
    return match.group(0)


def test_every_setting_the_policy_reads_is_declared():
    names = set(re.findall(r"settings\.([A-Z][A-Z0-9_]+)", _policy_body()))
    assert names, "the policy reads no settings"
    config = _read("config.py")
    missing = sorted(n for n in names
                     if not re.search(rf"^\s+{n}\s*:", config, re.M))
    assert not missing, f"config.py no longer declares: {missing}"


def test_every_path_the_page_renders_is_served():
    served = set(re.findall(r'"([a-z_]+)":', _policy_body()))
    js = _read("frontend/js/home.js")
    rendered = set(re.findall(r"'retention\.([a-z_]+)'", js))
    assert rendered, "home.js renders no retention.* path"
    missing = sorted(rendered - served)
    assert not missing, f"home.js renders retention keys /api/stats does not return: {missing}"


def test_every_tile_the_script_writes_exists_in_the_markup():
    js = _read("frontend/js/home.js")
    html = _read("frontend/home.html")
    tiles = re.findall(r"\['(rt-[a-z]+)',\s*'retention\.", js)
    assert tiles, "RETENTION_TILES is gone"
    for tile in tiles:
        assert f'id="{tile}"' in html, f"{tile} has no element in home.html"
        assert f'id="{tile}-unit"' in html, f"{tile}-unit has no element in home.html"
    assert "renderRetention(stats)" in js, "renderAll no longer calls renderRetention"
    assert "'retention-panel'" in js, "retention-panel is not in DATA_PANELS"
    assert 'id="retention-panel"' in html


def test_api_container_receives_the_backup_retention_it_reports():
    compose = _read("docker/docker-compose.prod.yml")
    api_block = compose.split("  face_recognition:", 1)[1].split("\n  backup:", 1)[0]
    assert re.search(r"^\s+BACKUP_RETENTION_DAYS:", api_block, re.M), (
        "face_recognition does not receive BACKUP_RETENTION_DAYS, so the home "
        "page would show config.py's default instead of the backup service's value")
