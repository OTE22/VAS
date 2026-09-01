"""Agent artifact registry: storage safety, ownership, retention.

The agent can now persist a document it generated. That creates two new ways
to lose control of surveillance output, and both are pinned here.

  1. A file under a path the caller influenced. Every stored path is
     server-generated ('<uuid>.<ext>') and re-anchored inside ARTIFACTS_DIR
     after realpath resolution, so neither a planner LLM nor a request body
     can name a location.

  2. One user reading another user's report. Ownership is answered by the
     DATABASE, and the download route returns a byte-identical 404 for
     missing, foreign and soft-deleted ids — a distinguishable response would
     let a signed-in user enumerate someone else's reports by id. This is
     also why artifacts are NOT served through GET /storage/{path}: that route
     authenticates but performs no ownership check at all.

Run inside the api container:

    docker exec face_recognition_api python -m pytest tests/test_agent_artifacts.py -v

Rows and files created here are created by this file and removed by this file
in a finally; nothing pre-existing is touched.
"""

import ast
import io
import json
import os
import time
import urllib.error
import urllib.request
import uuid as uuid_mod

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES_PATH = os.path.join(REPO, "sql_agent", "api", "routes.py")

# NOT `from tests.conftest import ...`: pytest imports the root
# conftest as the top-level module `conftest`, so importing it again
# under a package path creates a SECOND module object with its own
# SHARED_LOOP. The database engine then gets bound to one loop and
# used from the other, and unrelated suites fail with
# "attached to a different loop".
from conftest import run_on_shared_loop  # noqa: E402

from config import settings  # noqa: E402
from sql_agent.services import artifact_registry  # noqa: E402


PAYLOAD = b"SECURITY INTELLIGENCE REPORT\n" + b"x" * 512


async def _db():
    """A session, initializing the manager on first use.

    Pytest is not the app: nothing has run the startup hook, so db_manager has
    no session_maker until something asks for one.
    """
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    return db_manager.get_session()


# --------------------------------------------------------------- path safety

def test_stored_paths_are_server_generated_and_typed():
    """The extension comes from the type map, never from a caller."""
    artifact_id = uuid_mod.uuid4()
    assert artifact_registry.storage_path_for(artifact_id, "pdf") == f"{artifact_id}.pdf"
    assert artifact_registry.storage_path_for(artifact_id, "word") == f"{artifact_id}.docx"
    with pytest.raises(artifact_registry.ArtifactError):
        artifact_registry.storage_path_for(artifact_id, "sh")


@pytest.mark.parametrize("escape", [
    "../secrets.pdf",
    "../../etc/passwd",
    "sub/../../outside.pdf",
    os.path.join(os.sep, "etc", "passwd"),
])
def test_a_path_outside_the_artifacts_root_is_refused(escape):
    """Containment is asserted AFTER resolution, against a FIXED base.

    Checking a path against a directory that was itself built from the
    untrusted component is the bug this codebase has already been bitten by;
    an absolute path must also lose, which it only does because os.path.join
    with an absolute second argument is caught by the same realpath check.
    """
    candidate = os.path.join(artifact_registry.artifacts_root(), escape)
    with pytest.raises(artifact_registry.ArtifactError):
        artifact_registry._assert_inside_artifacts(candidate)


def test_the_artifacts_root_itself_is_not_a_valid_artifact_path():
    """Equal-to-root must not pass a prefix test as a stored file."""
    root = artifact_registry.artifacts_root()
    # The root resolves to itself and is allowed as a *directory*, but no
    # storage_path ever produces it, so a row can never point at it.
    assert artifact_registry._assert_inside_artifacts(root) == root
    assert not any(artifact_registry.storage_path_for(uuid_mod.uuid4(), t) == ""
                   for t in ("pdf", "word", "report"))


def test_a_truncated_render_is_never_stored():
    """An empty document is a failed render wearing a success mask."""
    with pytest.raises(artifact_registry.ArtifactError):
        artifact_registry.commit_bytes(uuid_mod.uuid4(), "pdf", b"")
    with pytest.raises(artifact_registry.ArtifactError):
        artifact_registry.commit_bytes(uuid_mod.uuid4(), "pdf", b"tiny")


def test_commit_leaves_no_partial_file_under_the_final_name():
    """tmp -> fsync -> os.replace: readers never see a half-written document."""
    artifact_id = uuid_mod.uuid4()
    relative = artifact_registry.commit_bytes(artifact_id, "report", PAYLOAD)
    final_path = os.path.join(artifact_registry.artifacts_root(), relative)
    try:
        assert os.path.isfile(final_path)
        with io.open(final_path, "rb") as handle:
            assert handle.read() == PAYLOAD
        # nothing left behind in the staging directory
        temp_dir = settings.ARTIFACTS_TEMP_DIR
        if os.path.isdir(temp_dir):
            assert f"{artifact_id}.part" not in os.listdir(temp_dir)
    finally:
        artifact_registry.delete_file(relative)


def test_delete_file_refuses_to_follow_a_traversing_relative_path():
    """A tampered storage_path must not make cleanup delete arbitrary files."""
    victim = os.path.join(REPO, "tests", f".artifact_victim_{uuid_mod.uuid4().hex}")
    with io.open(victim, "wb") as handle:
        handle.write(b"do not delete me")
    try:
        relative = os.path.relpath(victim, artifact_registry.artifacts_root())
        artifact_registry.delete_file(relative)      # never raises, must not delete
        assert os.path.exists(victim), "delete_file followed a traversing path"
    finally:
        try:
            os.remove(victim)
        except OSError:
            pass


# ------------------------------------------------------- ownership (database)

class _Artifacts:
    """Rows created by this test module, torn down together."""

    def __init__(self):
        self.ids = []

    async def make(self, db, *, user_id, artifact_type="report", **kwargs):
        row = await artifact_registry.register_artifact(
            db, payload=PAYLOAD, artifact_type=artifact_type,
            title="Test Report", language="en", user_id=user_id,
            created_by_username="pytest", **kwargs)
        self.ids.append((row.id, row.storage_path))
        return row


@pytest.fixture
def artifacts():
    holder = _Artifacts()
    yield holder

    async def _cleanup():
        from sqlalchemy import text as sa_text
        async with await _db() as db:
            for artifact_id, _path in holder.ids:
                await db.execute(sa_text("DELETE FROM agent_artifacts WHERE id = :i"),
                                 {"i": artifact_id})
            await db.commit()
        for _artifact_id, path in holder.ids:
            artifact_registry.delete_file(path)

    run_on_shared_loop(_cleanup())


def test_ownership_is_answered_by_the_database_not_the_caller(artifacts):
    """A correct id belonging to someone else is simply not found."""
    async def _check():
        async with await _db() as db:
            row = await artifacts.make(db, user_id=1)
            await db.commit()
            assert await artifact_registry.get_owned_artifact(db, row.id, 1) is not None
            # the SAME id, a different owner
            assert await artifact_registry.get_owned_artifact(db, row.id, 2) is None
            # no owner at all, e.g. an unauthenticated path that got this far
            assert await artifact_registry.get_owned_artifact(db, row.id, None) is None

    run_on_shared_loop(_check())


def test_a_malformed_id_is_not_found_rather_than_an_error(artifacts):
    """'../../etc/passwd' as an id must 404, not raise a 500."""
    async def _check():
        async with await _db() as db:
            for bad in ("../../etc/passwd", "not-a-uuid", "", "1 OR 1=1"):
                assert await artifact_registry.get_owned_artifact(db, bad, 1) is None

    run_on_shared_loop(_check())


def test_a_soft_deleted_artifact_stops_resolving(artifacts):
    async def _check():
        from datetime import datetime
        async with await _db() as db:
            row = await artifacts.make(db, user_id=1)
            await db.commit()
            row.deleted_at = datetime.utcnow()
            await db.commit()
            assert await artifact_registry.get_owned_artifact(db, row.id, 1) is None

    run_on_shared_loop(_check())


def test_the_candidate_list_never_carries_report_content(artifacts):
    """list_recent_artifacts feeds a PROMPT and an API response.

    source_content is the rendered narrative — the same surveillance text the
    file holds. It exists for translation lineage and must not travel into
    either, or the ownership boundary the download route enforces is moot.
    """
    async def _check():
        async with await _db() as db:
            secret = "CONFIDENTIAL-NARRATIVE-" + uuid_mod.uuid4().hex
            await artifacts.make(db, user_id=1, source_content=secret,
                                 source_sql="SELECT * FROM detections")
            await db.commit()
            candidates = await artifact_registry.list_recent_artifacts(db, 1, limit=5)
            assert candidates, "the artifact just created should be a candidate"
            blob = repr(candidates)
            assert secret not in blob
            assert "source_content" not in blob
            assert "SELECT" not in blob.upper()
            # a different user sees none of them
            mine = {str(a[0]) for a in artifacts.ids}
            theirs = await artifact_registry.list_recent_artifacts(db, 999999)
            assert mine.isdisjoint({c["artifact_id"] for c in theirs})

    run_on_shared_loop(_check())


def test_a_failed_registration_leaves_neither_a_file_nor_a_row(monkeypatch):
    """The two halves fail together or not at all.

    An orphan FILE is surveillance output no retention sweep will ever find,
    because retention walks rows. An orphan ROW is worse: every later
    reference to it resolves to a document that is not there.

    The row half is the subtle one. db.add() makes the object pending in the
    CALLER's session, and db_manager.get_session() commits that session when
    the request ends — so a caller that simply catches this failure and
    apologises to the user would still have the row written, pointing at the
    file we just deleted. Registration therefore expunges before unlinking,
    and this test outlives the session to prove it.
    """
    title = "doomed-" + uuid_mod.uuid4().hex

    async def _check():
        from sqlalchemy import text as sa_text
        before = set(os.listdir(artifact_registry.artifacts_root()))

        # The session is ENTERED AND EXITED here: exiting is what commits, so
        # asserting inside it would miss the resurrected row entirely.
        async with await _db() as db:
            async def _boom(*_args, **_kwargs):
                raise RuntimeError("injected insert failure")

            monkeypatch.setattr(db, "flush", _boom)
            with pytest.raises(RuntimeError):
                await artifact_registry.register_artifact(
                    db, payload=PAYLOAD, artifact_type="report", title=title,
                    language="en", user_id=1, created_by_username="pytest")

        after = set(os.listdir(artifact_registry.artifacts_root()))
        assert after == before, f"orphan file left behind: {after - before}"

        async with await _db() as db:
            surviving = (await db.execute(sa_text(
                "SELECT count(*) FROM agent_artifacts WHERE title = :t"),
                {"t": title})).scalar()
        assert surviving == 0, (
            "a row survived a failed registration — the caller's session "
            "committed the pending object after the file was deleted")

    run_on_shared_loop(_check())


# -------------------------------------------------------- the download route

def _route_source():
    with io.open(ROUTES_PATH, encoding="utf-8") as handle:
        return handle.read()


def _download_route_node():
    tree = ast.parse(_route_source())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and node.name == "download_artifact":
            return node
    pytest.fail("download_artifact route not found in routes.py")


def test_the_download_route_requires_chatbot_access():
    node = _download_route_node()
    decorators = ast.dump(ast.Module(body=list(node.decorator_list), type_ignores=[]))
    assert "artifacts/{artifact_id}" in decorators
    args = ast.dump(ast.Module(body=[ast.Expr(value=a) for a in
                                     (node.args.defaults or [])], type_ignores=[]))
    assert "require_chatbot_access" in args, "download route is not access-gated"


def test_the_download_route_resolves_the_path_from_the_database_only():
    """The id names a ROW; the row names the path. No caller path ever."""
    body = ast.dump(_download_route_node())
    assert "get_owned_artifact" in body, "ownership is not re-checked against the DB"
    assert "_assert_inside_artifacts" in body, "stored path is not re-anchored"


def test_missing_foreign_and_deleted_all_return_the_same_404():
    """One shared HTTPException object — the responses cannot drift apart.

    Distinguishable errors would turn the download route into an oracle for
    'does artifact <id> exist', which is exactly what ownership is meant to
    hide.
    """
    node = _download_route_node()
    shared, inline = [], []
    for raise_node in (n for n in ast.walk(node) if isinstance(n, ast.Raise)):
        if isinstance(raise_node.exc, ast.Name):
            shared.append(raise_node.exc.id)
        else:
            # A raise built in place is exactly how the responses drift apart,
            # so each one has to justify its own status. Only the
            # authentication guard may differ from the shared 404.
            status = None
            for keyword in getattr(raise_node.exc, "keywords", []):
                if keyword.arg == "status_code" and isinstance(keyword.value, ast.Constant):
                    status = keyword.value.value
            inline.append(status)

    assert set(shared) == {"not_found"}, (
        f"the route raises a name other than the shared 404: {set(shared)}")
    assert len(shared) >= 3, "expected the 404 on missing, escaping and file-less rows"
    assert all(s == 401 for s in inline), (
        f"an outcome answers with its own status instead of the shared 404: {inline}")


def test_artifacts_are_not_served_through_the_unowned_storage_route():
    """GET /storage/{path} authenticates but does not check ownership.

    Any artifact URL handed to a client must point at the id route. If this
    ever fails, every user can read every user's reports.
    """
    source = _route_source()
    node = _download_route_node()
    # The docstring explains *why* /storage/ is avoided, so match the CODE:
    # rebuild the body with the docstring dropped.
    body = [n for n in node.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    segments = [ast.get_source_segment(source, n) or "" for n in body]
    assert "/storage/" not in "\n".join(segments)


# -------------------------------------------------------------- retention

def test_retention_deletes_the_file_with_the_row():
    """A report must not outlive the detections it was rendered from."""
    from backend.core.data_retention import DataRetentionManager

    async def _check():
        from datetime import datetime, timedelta
        from sqlalchemy import text as sa_text

        artifact_id = uuid_mod.uuid4()
        relative = artifact_registry.commit_bytes(artifact_id, "report", PAYLOAD)
        full_path = os.path.join(artifact_registry.artifacts_root(), relative)
        expired = datetime.utcnow() - timedelta(
            days=int(settings.DATA_RETENTION_DAYS) + 5)
        try:
            async with await _db() as db:
                await db.execute(sa_text(
                    "INSERT INTO agent_artifacts"
                    " (id, user_id, created_by_username, type, title, language,"
                    "  storage_path, created_at)"
                    " VALUES (:i, NULL, 'pytest', 'report', 'expired', 'en', :p, :c)"),
                    {"i": artifact_id, "p": relative, "c": expired})
                await db.commit()

            assert os.path.isfile(full_path)
            async with await _db() as db:
                result = await DataRetentionManager()._cleanup_agent_artifacts(
                    db, dry_run=False)
                await db.commit()

            assert result["agent_artifacts_deleted"] >= 1
            assert not os.path.exists(full_path), "the row went, the FILE stayed"
            async with await _db() as db:
                still = (await db.execute(sa_text(
                    "SELECT count(*) FROM agent_artifacts WHERE id = :i"),
                    {"i": artifact_id})).scalar()
                assert still == 0
        finally:
            artifact_registry.delete_file(relative)
            async with await _db() as db:
                await db.execute(sa_text("DELETE FROM agent_artifacts WHERE id = :i"),
                                 {"i": artifact_id})
                await db.commit()

    run_on_shared_loop(_check())


def test_retention_dry_run_deletes_nothing():
    """A dry run that deletes is worse than no dry run."""
    from backend.core.data_retention import DataRetentionManager

    async def _check():
        from datetime import datetime, timedelta
        from sqlalchemy import text as sa_text

        artifact_id = uuid_mod.uuid4()
        relative = artifact_registry.commit_bytes(artifact_id, "report", PAYLOAD)
        full_path = os.path.join(artifact_registry.artifacts_root(), relative)
        expired = datetime.utcnow() - timedelta(
            days=int(settings.DATA_RETENTION_DAYS) + 5)
        try:
            async with await _db() as db:
                await db.execute(sa_text(
                    "INSERT INTO agent_artifacts"
                    " (id, user_id, created_by_username, type, title, language,"
                    "  storage_path, created_at)"
                    " VALUES (:i, NULL, 'pytest', 'report', 'expired', 'en', :p, :c)"),
                    {"i": artifact_id, "p": relative, "c": expired})
                await db.commit()

            async with await _db() as db:
                result = await DataRetentionManager()._cleanup_agent_artifacts(
                    db, dry_run=True)
            assert result["agent_artifacts_deleted"] >= 1
            assert os.path.isfile(full_path), "dry run deleted the file"
            async with await _db() as db:
                still = (await db.execute(sa_text(
                    "SELECT count(*) FROM agent_artifacts WHERE id = :i"),
                    {"i": artifact_id})).scalar()
                assert still == 1, "dry run deleted the row"
        finally:
            artifact_registry.delete_file(relative)
            async with await _db() as db:
                await db.execute(sa_text("DELETE FROM agent_artifacts WHERE id = :i"),
                                 {"i": artifact_id})
                await db.commit()

    run_on_shared_loop(_check())


def test_retention_sweeps_abandoned_part_files():
    """A process that died mid-commit leaves a .part nothing will ever claim."""
    from backend.core.data_retention import DataRetentionManager

    temp_dir = settings.ARTIFACTS_TEMP_DIR
    os.makedirs(temp_dir, exist_ok=True)
    stale = os.path.join(temp_dir, f"{uuid_mod.uuid4()}.part")
    fresh = os.path.join(temp_dir, f"{uuid_mod.uuid4()}.part")
    try:
        for path in (stale, fresh):
            with io.open(path, "wb") as handle:
                handle.write(PAYLOAD)
        old = time.time() - 200000
        os.utime(stale, (old, old))

        removed = DataRetentionManager()._cleanup_artifact_parts_sync()

        assert removed >= 1
        assert not os.path.exists(stale)
        assert os.path.exists(fresh), "an in-flight render was swept out from under it"
    finally:
        for path in (stale, fresh):
            try:
                os.remove(path)
            except OSError:
                pass


# ------------------------------------------------------------ block plumbing

def test_artifact_is_an_allowed_content_block_type():
    """Without this the message pipeline rejects the block outright."""
    from backend.services.conversation_service import _ALLOWED_BLOCK_TYPES
    assert "artifact" in _ALLOWED_BLOCK_TYPES


# ------------------------------------------------- exports persist (phase 3)

BASE = "http://localhost:8000"


def _post(path, body, token):
    """Returns (status, body, headers).

    `headers` is the email.message.Message, NOT dict(...): Starlette lowercases
    every header name on the wire, so a dict makes case-sensitive lookups like
    headers["X-Artifact-Id"] fail on a response that does contain it.
    """
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


@pytest.fixture(scope="module")
def token():
    body = json.dumps({"username": "admin", "password": "admin123"}).encode()
    req = urllib.request.Request(BASE + "/api/auth/login", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


@pytest.fixture
def exported():
    """Artifacts produced by the export endpoint during a test, cleaned after."""
    created = []
    yield created

    async def _cleanup():
        from sqlalchemy import text as sa_text
        paths = []
        async with await _db() as db:
            for artifact_id in created:
                path = (await db.execute(sa_text(
                    "SELECT storage_path FROM agent_artifacts WHERE id = :i"),
                    {"i": uuid_mod.UUID(artifact_id)})).scalar()
                if path:
                    paths.append(path)
                await db.execute(sa_text("DELETE FROM agent_artifacts WHERE id = :i"),
                                 {"i": uuid_mod.UUID(artifact_id)})
            await db.commit()
        for path in paths:
            artifact_registry.delete_file(path)

    run_on_shared_loop(_cleanup())


EXPORT_BODY = {"content": "SECURITY INTELLIGENCE SECTION\n\nSubject seen at Gate 3.",
               "title": "Weekly Report", "timestamp": "2026-08-29T00:00:00Z"}


@pytest.mark.parametrize("fmt,magic,media", [
    ("pdf", b"%PDF-", "application/pdf"),
    ("word", b"PK", "application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"),
])
def test_the_export_response_is_unchanged_and_now_names_its_artifact(
        fmt, magic, media, token, exported):
    """Persistence is ADDITIVE: same bytes, same headers, plus an id.

    The rendering code moved out of routes.py in this phase. If the move
    changed what the endpoint returns, every existing client breaks — so the
    document contract is asserted here alongside the new header.
    """
    status, body, headers = _post(f"/api/sql-agent/export/{fmt}", EXPORT_BODY, token)
    assert status == 200, body[:300]
    assert body.startswith(magic), "the endpoint stopped returning a real document"
    assert (headers.get("Content-Type") or "").startswith(media)
    assert 'filename="Intelligence_Report_' in (headers.get("Content-Disposition") or "")

    artifact_id = headers.get("X-Artifact-Id")
    assert artifact_id, "the export was not registered as an artifact"
    exported.append(artifact_id)
    assert "X-Artifact-Id" in (headers.get("Access-Control-Expose-Headers") or ""), \
        "the browser cannot read the header unless it is exposed"


def test_an_exported_document_can_be_downloaded_by_its_id(token, exported):
    """The id is only useful if it resolves — row AND file, same bytes."""
    status, body, headers = _post("/api/sql-agent/export/pdf", EXPORT_BODY, token)
    assert status == 200
    artifact_id = headers["X-Artifact-Id"]
    exported.append(artifact_id)

    req = urllib.request.Request(f"{BASE}/api/sql-agent/artifacts/{artifact_id}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        downloaded = r.read()
    assert downloaded == body, "the stored file is not the document that was served"


def test_a_persistence_failure_costs_the_header_not_the_document(monkeypatch, exported):
    """The user asked for a document, and the document exists.

    Losing it because a bookkeeping row failed would trade a working feature
    for an outage. The header must simply be absent — and it must never name
    a row that was not written.

    Called IN-PROCESS, not over HTTP: the live server is a separate process
    that a monkeypatch here cannot reach, so an HTTP version of this test
    would patch nothing and pass no matter what the code does.
    """
    from sql_agent.api import routes

    class _Req:
        content = EXPORT_BODY["content"]
        title = EXPORT_BODY["title"]
        timestamp = EXPORT_BODY["timestamp"]

    class _User:
        id = 1
        username = "admin"

    payload = b"%PDF-1.4 not a real document, but bytes the caller already holds"

    # Positive control FIRST: unpatched, the header must appear. Without this
    # the negative assertion below would also pass if the header never
    # appeared at all.
    async def _healthy():
        return await routes._respond_with_export(
            payload, "pdf", "Header Control", "2026-08-29", _Req(), _User())

    good = run_on_shared_loop(_healthy())
    assert good.headers.get("X-Artifact-Id"), "control failed: no header on success"
    exported.append(good.headers["X-Artifact-Id"])

    async def _fail(*_args, **_kwargs):
        raise RuntimeError("injected registration failure")

    monkeypatch.setattr(routes, "render_and_register", _fail)

    async def _broken():
        return await routes._respond_with_export(
            payload, "pdf", "Header Control", "2026-08-29", _Req(), _User())

    response = run_on_shared_loop(_broken())
    assert response.body == payload, "the document must still be served in full"
    assert response.media_type == "application/pdf"
    assert "X-Artifact-Id" not in response.headers, (
        "the response named an artifact that was never persisted")


def test_no_api_response_ever_carries_the_documents_source_content(token, exported):
    """source_content is the narrative the document was rendered from.

    It is stored for translation lineage. Serializing it would hand the report
    text to any endpoint that lists artifacts, bypassing the download route's
    ownership check entirely.
    """
    secret = "CONFIDENTIAL-NARRATIVE-" + uuid_mod.uuid4().hex
    body = dict(EXPORT_BODY, content=EXPORT_BODY["content"] + "\n" + secret)
    status, _payload, headers = _post("/api/sql-agent/export/pdf", body, token)
    assert status == 200
    artifact_id = headers["X-Artifact-Id"]
    exported.append(artifact_id)

    async def _check():
        from sqlalchemy import text as sa_text
        async with await _db() as db:
            stored = (await db.execute(sa_text(
                "SELECT source_content FROM agent_artifacts WHERE id = :i"),
                {"i": uuid_mod.UUID(artifact_id)})).scalar()
            assert stored and secret in stored, "lineage was not recorded at all"
            candidates = await artifact_registry.list_recent_artifacts(
                db, 1, limit=5)
        assert secret not in repr(candidates)

    run_on_shared_loop(_check())


def test_the_recorded_language_matches_what_the_document_renders_as(token, exported):
    """A translation request needs to know where the document starts."""
    arabic = dict(EXPORT_BODY, content="تقرير أمني عن الشخص المتعقب عند البوابة",
                  title="Arabic Report")
    status, _payload, headers = _post("/api/sql-agent/export/pdf", arabic, token)
    assert status == 200
    artifact_id = headers["X-Artifact-Id"]
    exported.append(artifact_id)

    async def _check():
        from sqlalchemy import text as sa_text
        async with await _db() as db:
            language = (await db.execute(sa_text(
                "SELECT language FROM agent_artifacts WHERE id = :i"),
                {"i": uuid_mod.UUID(artifact_id)})).scalar()
        assert language == "ar", f"Arabic document recorded as {language!r}"

    run_on_shared_loop(_check())


def test_a_stale_lineage_pointer_costs_the_lineage_never_the_artifact():
    """Registration survives working memory pointing at a deleted row.

    Working memory durably records the history row a result came from;
    retention, user deletion or a test teardown can delete that row while
    the pointer lives on in the session file. Registration then died on
    agent_artifacts_source_result_id_fkey — and because the stale pointer
    persisted, EVERY later document failed too, silently ("I built the
    document but couldn't save it"). Seen live on 2026-08-30.

    The contract: retry once with the optional lineage stripped. The user
    gets their document; only the dangling provenance link is lost — which
    is the truthful outcome, since the row it named no longer exists.
    """
    from sql_agent.services.export_builders import render_and_register

    async def _check():
        from sqlalchemy import text as sa_text
        async with await _db() as db:
            missing_history_id = (await db.execute(sa_text(
                "SELECT coalesce(max(id), 0) + 1000000 FROM user_query_history"
            ))).scalar()

            artifact_id = await render_and_register(
                db, payload=PAYLOAD, artifact_type="report",
                title="stale-lineage probe", language="en", user_id=1,
                created_by_username="pytest",
                source_content="narrative",
                source_result_id=int(missing_history_id),   # dangling on purpose
            )
        assert artifact_id, (
            "a dangling source_result_id lost the ARTIFACT — the retry "
            "without lineage did not happen")

        async with await _db() as db:
            row = (await db.execute(sa_text(
                "SELECT source_result_id, storage_path FROM agent_artifacts "
                "WHERE id = CAST(:i AS uuid)"), {"i": artifact_id})).fetchone()
            assert row is not None, "the artifact id does not resolve to a row"
            assert row[0] is None, "the dangling reference was stored anyway"
            # cleanup
            artifact_registry.delete_file(row[1])
            await db.execute(sa_text(
                "DELETE FROM agent_artifacts WHERE id = CAST(:i AS uuid)"),
                {"i": artifact_id})
            await db.commit()

    run_on_shared_loop(_check())


def test_one_persistence_path_serves_both_the_endpoint_and_the_agent():
    """P5's node and the HTTP export must not grow separate semantics.

    Two persistence paths is how a row-without-a-file appears on one of them
    six months from now. The registry is reached through render_and_register
    and nowhere else outside the registry module itself.
    """
    import pathlib
    offenders = []
    root = pathlib.Path(REPO)
    for path in list(root.glob("sql_agent/**/*.py")) + list(root.glob("backend/**/*.py")):
        if path.name in ("artifact_registry.py", "export_builders.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "register_artifact(" in text and "render_and_register" not in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"these call the registry directly: {offenders}"
