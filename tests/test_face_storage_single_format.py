"""
One writer, one folder format, one config value, one DB representation.
=======================================================================
Run inside the api container:

    docker exec face_recognition_api python -m pytest \
        tests/test_face_storage_single_format.py -v

These are PRE-PURGE tests: they assert BEHAVIOUR ("nothing writes the legacy
format any more"), never destroyed state. The legacy files still exist on disk
until an operator runs `purge_face_storage.py --apply`, so assertions about
emptiness live in that script's `--verify` mode instead — putting them here
would make the suite fail for a reason that is not a defect.

Background: face storage had three shapes at once — flat `assets/faces/*.jpg`,
`storage/faces/<display_name>/imageN.ext`, and the target
`storage/faces/<identity_uuid>/image_NNN.ext` — kept alive by two promotion
writers, a loader that turned folder names into people, and a stale
`FACES_DIR=./assets/faces` in .env.
"""

import ast
import io
import os
import re
import subprocess
import uuid as uuid_module

import pytest

from conftest import run_on_shared_loop as run_async

FACES_DIR = "/app/storage/faces"
ASSETS_DIR = "/app/assets"
SERVICE_SRC = "/app/backend/core/enrollment_service.py"
IDENTITY_SERVICE_SRC = "/app/backend/core/identity_service.py"
LOADER_SRC = "/app/backend/core/identity_loader.py"
PURGE_SCRIPT = "/app/scripts/purge_face_storage.py"

UUID_FOLDER_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
IMAGE_NAME_RE = re.compile(r"^image_\d{3}\.(jpg|png|webp|bmp)$")


def _source(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def _code_only(source):
    """Strip docstrings so contract scans read code, not prose."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return ast.unparse(tree)


def _tree_stats(path):
    if not os.path.isdir(path):
        return 0, 0
    files = total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                pass
    return files, total


# ---------------------------------------------------------------------------
# One configuration value
# ---------------------------------------------------------------------------

def test_faces_dir_is_the_single_canonical_location():
    from config import settings

    assert os.path.realpath(settings.FACES_DIR) == os.path.realpath(
        os.path.join(settings.STORAGE_DIR, "faces")), (
        "FACES_DIR must be <STORAGE_DIR>/faces")
    assert "assets" not in os.path.realpath(settings.FACES_DIR).split(os.sep)


def test_no_divergent_faces_dir_path_fallbacks_remain():
    """Six call sites once supplied three different PATH defaults, five of them
    relative while the real value is absolute — including the guard that keeps
    the retention sweeper away from enrollment photos.

    A path-shaped default is the defect: it silently substitutes a different
    directory when the setting is missing. config_guard is exempt by design —
    it validates arbitrary config objects through getattr and defaults to the
    empty string, which it reports as FACES_DIR_MISSING rather than papering
    over.
    """
    # default is anything up to the closing paren; flag it only when it names a path
    pattern = re.compile(
        r"getattr\(\s*\w+\s*,\s*['\"]FACES_DIR['\"]\s*,\s*([^)]*)\)")
    offenders = []
    for root, _dirs, names in os.walk("/app/backend"):
        if "__pycache__" in root:
            continue
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            if path.endswith("security/config_guard.py"):
                continue
            source = _source(path)
            for match in pattern.finditer(source):
                default = match.group(1).strip()
                if "/" in default or "\\" in default or "faces" in default.lower():
                    line = source[:match.start()].count("\n") + 1
                    offenders.append(f"{path}:{line} -> {default}")
    assert not offenders, (
        "FACES_DIR still has path-shaped fallback defaults that can disagree "
        f"with the configured value: {offenders}")


def test_config_guard_rejects_legacy_face_paths():
    """FACES_DIR used to be a settable field checked against <STORAGE_DIR>/faces.

    It is now derived and has no setter, so the legacy paths cannot be reached
    through configuration at all. What remains reachable — and what this asserts
    — is a stale FACES_DIR left in the environment: it is reported rather than
    ignored, because an operator reading their compose file would otherwise
    believe face images live somewhere they do not.
    """
    from types import SimpleNamespace

    from backend.security.config_guard import collect_violations, fatal_only

    def codes(**env):
        cfg = SimpleNamespace(ENVIRONMENT="development", JWT_ALGORITHM="HS256",
                              AUTH_COOKIE_SAMESITE="lax", CORS_ORIGINS="",
                              STORAGE_DIR="/app/storage")
        return {v.code for v in collect_violations(cfg, env=env)}

    assert "DERIVED_PATH_OVERRIDE" in codes(FACES_DIR="/app/assets/faces")
    assert "DERIVED_PATH_OVERRIDE" in codes(FACES_DIR="./assets/faces")
    assert "DERIVED_PATH_OVERRIDE" not in codes()

    # Fatal, not advisory — and in development too, because a gallery split
    # across two directories is a data-integrity fault in any environment.
    violations = collect_violations(
        SimpleNamespace(ENVIRONMENT="development", JWT_ALGORITHM="HS256",
                        AUTH_COOKIE_SAMESITE="lax", CORS_ORIGINS="",
                        STORAGE_DIR="/app/storage"),
        env={"FACES_DIR": "/app/assets/faces"})
    assert "DERIVED_PATH_OVERRIDE" in {v.code for v in fatal_only(violations)}


def test_storage_paths_are_not_runtime_mutable():
    from backend.security.config_guard import SECURITY_CRITICAL_KEYS

    for key in ("FACES_DIR", "STORAGE_DIR", "IDENTITY_INDEX_DB_PATH"):
        assert key in SECURITY_CRITICAL_KEYS


# ---------------------------------------------------------------------------
# One writer — no display-name folders, no identities from folder names
# ---------------------------------------------------------------------------

def test_promotion_no_longer_builds_folders_from_display_names():
    code = _code_only(_source(IDENTITY_SERVICE_SRC))
    for token in ("safe_name", "person_folder"):
        assert token not in code, (
            f"identity_service still derives a folder from a display name ({token})")
    assert "adopt_existing_file" in code, (
        "promotion must delegate placement to the shared enrollment helper")


def test_loader_never_creates_identities_from_folder_names():
    code = _code_only(_source(LOADER_SRC))
    assert "identity = Identity(" not in code and "db.add(identity)" not in code, (
        "the loader can still create an identity from disk — a restart would "
        "resurrect purged people")
    # It resolves by id, not by display_name.
    assert "Identity.display_name ==" not in code, (
        "the loader still resolves people by display name")
    assert "Identity.id == uuid.UUID(folder_uuid)" in code


def test_only_the_enrollment_service_places_enrollment_files():
    """os.replace/copy2 into FACES_DIR happens in exactly one module."""
    writers = []
    for root, _dirs, names in os.walk("/app/backend"):
        if "__pycache__" in root:
            continue
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            code = _code_only(_source(path))
            if ("identity_folder(" in code
                    and ("os.replace(" in code or "shutil.copy2(" in code)):
                writers.append(path)
    assert writers == [SERVICE_SRC], (
        f"expected enrollment_service to be the only placer, found {writers}")


def test_no_code_writes_into_assets():
    """assets/ is a read-only mount and must never receive uploads."""
    offenders = []
    for root, _dirs, names in os.walk("/app/backend"):
        if "__pycache__" in root:
            continue
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            code = _code_only(_source(path))
            for match in re.finditer(r"(imwrite|copy2|os\.replace|open)\s*\([^)]{0,120}", code):
                snippet = match.group(0)
                if "assets" in snippet:
                    offenders.append(f"{path}: {snippet[:80]}")
    assert not offenders, f"code writes into assets/: {offenders}"


def test_no_absolute_image_path_is_stored():
    """best_snapshot_path must hold the normalized relative form."""
    code = _code_only(_source(IDENTITY_SERVICE_SRC))
    assert "identity.best_snapshot_path = dest_path" not in code, (
        "promotion still stores an absolute filesystem path")
    assert "best_snapshot_path = adopted.storage_path" in code


# ---------------------------------------------------------------------------
# Live: uploads use UUID folders only
# ---------------------------------------------------------------------------

def test_live_upload_uses_uuid_folder_and_leaves_assets_untouched():
    """One end-to-end upload: lands in <uuid>/image_NNN.ext, writes nothing to
    assets/, and creates no display-name folder."""
    from backend.core import enrollment_service
    from backend.core.identity_index_pgvector import get_pgvector_index
    from backend.core.identity_service import IdentityService
    from db_connection import db_manager
    from sqlalchemy import text

    name = "qa_singlefmt_upload"
    fixture = "/app/tests/fixtures/faces/face_a.jpg"
    assets_before = _tree_stats(ASSETS_DIR)
    folders_before = set(os.listdir(FACES_DIR)) if os.path.isdir(FACES_DIR) else set()

    async def _cleanup():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            rows = (await db.execute(
                text("SELECT id FROM identities WHERE display_name = :n"),
                {"n": name})).all()
            for row in rows:
                ident = str(row[0])
                await db.execute(text("DELETE FROM identity_embeddings WHERE identity_id=:i"),
                                 {"i": ident})
                await db.execute(text("DELETE FROM identity_images WHERE identity_id=:i"),
                                 {"i": ident})
                await db.execute(text("DELETE FROM identities WHERE id=:i"), {"i": ident})
            await db.commit()
            return [str(r[0]) for r in rows]

    for stale in run_async(_cleanup()):
        import shutil
        shutil.rmtree(os.path.join(FACES_DIR, stale), ignore_errors=True)

    async def _run():
        async with db_manager.get_session() as db:
            with open(fixture, "rb") as handle:
                payload = handle.read()
            return await enrollment_service.enroll_image(
                db, image_bytes=payload, original_filename="face_a.jpg",
                content_type="image/jpeg", person_name=name)

    original = enrollment_service._identity_service
    enrollment_service._identity_service = (
        lambda: IdentityService(None, pgvector_index=get_pgvector_index()))
    try:
        result = run_async(_run())
    finally:
        enrollment_service._identity_service = original

    try:
        identity_id = result.identity_id
        assert UUID_FOLDER_RE.match(identity_id)
        folder = os.path.join(FACES_DIR, identity_id)
        assert os.path.isdir(folder), "upload did not create the UUID folder"
        entries = os.listdir(folder)
        assert len(entries) == 1 and IMAGE_NAME_RE.match(entries[0]), entries
        assert result.storage_path == f"storage/faces/{identity_id}/{entries[0]}"
        assert not os.path.isabs(result.storage_path)
        assert result.source_type == "upload"

        # No display-name folder appeared anywhere.
        new_folders = set(os.listdir(FACES_DIR)) - folders_before
        assert new_folders == {identity_id}, (
            f"upload created unexpected folders: {new_folders - {identity_id}}")

        # assets/ is untouched, byte for byte.
        assert _tree_stats(ASSETS_DIR) == assets_before, "upload modified assets/"
    finally:
        for stale in run_async(_cleanup()):
            import shutil
            shutil.rmtree(os.path.join(FACES_DIR, stale), ignore_errors=True)


def test_every_existing_uuid_folder_holds_only_server_generated_names():
    """Whatever legacy folders remain, the UUID ones follow the one format."""
    if not os.path.isdir(FACES_DIR):
        pytest.skip("faces dir absent")
    problems = []
    for entry in os.listdir(FACES_DIR):
        if not UUID_FOLDER_RE.match(entry):
            continue          # legacy folder — removed by the purge, not by a test
        for name in os.listdir(os.path.join(FACES_DIR, entry)):
            if not IMAGE_NAME_RE.match(name):
                problems.append(f"{entry}/{name}")
    assert not problems, f"non-conforming filenames in UUID folders: {problems}"


# ---------------------------------------------------------------------------
# Purge tooling: dry-run is genuinely read-only
# ---------------------------------------------------------------------------

def _counts_and_files():
    from sqlalchemy import text
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            out = {}
            for table in ("identities", "identity_embeddings", "faces", "detections"):
                out[table] = (await db.execute(
                    text(f"SELECT count(*) FROM {table}"))).scalar()
            return out
    return run_async(_run()), _tree_stats(FACES_DIR), _tree_stats(ASSETS_DIR)


def test_dry_run_changes_nothing():
    before = _counts_and_files()
    result = subprocess.run([os.sys.executable, PURGE_SCRIPT, "--dry-run"],
                            capture_output=True, text=True, cwd="/app", timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "nothing has been deleted" in result.stdout.lower()
    assert "TOTAL BYTES TO REMOVE" in result.stdout
    assert _counts_and_files() == before, "dry-run mutated state"


def test_dry_run_reports_every_required_item():
    result = subprocess.run([os.sys.executable, PURGE_SCRIPT, "--dry-run"],
                            capture_output=True, text=True, cwd="/app", timeout=300)
    out = result.stdout
    for required in ("identities", "identity_embeddings", "identity_images",
                     "identity_appearances", "identity_relationships", "faces",
                     "detections", "enrollment files", "pipeline snapshots",
                     "debug crops", "webhook images", "vector-index artifacts",
                     # The demo-wipe extension: tables no FK cascade reaches,
                     # the chat history, and the stale volume shadow.
                     "ml_feature_snapshots", "threat_assessments",
                     "user_query_history", "conversation cache",
                     "stale legacy-volume files",
                     "TOTAL BYTES TO REMOVE"):
        assert required in out, f"dry-run report omits {required!r}"
    # Preserved tables are named explicitly so the operator can see the boundary.
    for preserved in ("users", "pipelines", "settings"):
        assert preserved in out


def test_apply_refuses_without_the_understanding_flag():
    result = subprocess.run([os.sys.executable, PURGE_SCRIPT, "--apply"],
                            capture_output=True, text=True, cwd="/app", timeout=300)
    assert result.returncode == 2
    assert "yes-i-understand" in (result.stdout + result.stderr)


def test_apply_aborts_when_the_confirmation_phrase_is_wrong():
    """Both flags present, wrong phrase typed -> abort, nothing deleted."""
    before = _counts_and_files()
    result = subprocess.run(
        [os.sys.executable, PURGE_SCRIPT, "--apply", "--yes-i-understand"],
        input="not the phrase\n", capture_output=True, text=True,
        cwd="/app", timeout=300)
    assert result.returncode == 1, result.stdout[-2000:]
    assert "ABORTED" in result.stdout
    assert _counts_and_files() == before, "a mismatched phrase still deleted data"


def test_purge_cannot_reach_outside_the_allowed_roots():
    sys_path_added = "/app" in os.sys.path or os.sys.path.insert(0, "/app")
    import importlib.util

    spec = importlib.util.spec_from_file_location("purge_mod", PURGE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    paths = module._paths()
    for hostile in ("/etc", "/app/frontend", "/app/weights", "/app/icons", "/"):
        with pytest.raises(SystemExit):
            module._assert_inside_allowed(hostile, paths)
    # ...while the real targets are permitted.
    for allowed in (paths["faces"], paths["assets_faces"], paths["cropped"]):
        module._assert_inside_allowed(allowed, paths)
