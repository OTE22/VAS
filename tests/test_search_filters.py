"""Advanced Search candidate selection: dedup, filter ordering, alert scoping.

Three defects motivated these tests, all confirmed against the running app
before the fix:

  1. `pipeline_id` was accepted by /api/search/advanced, threaded into
     search_multi_face and written to SearchHistory.filters, but no query ever
     read it. Filtering to a camera the matched face had never been seen on
     returned the IDENTICAL result set.

  2. Candidates were truncated to top_k BEFORE the date predicate was applied,
     so a narrow range silently shrank an already-cut list instead of promoting
     the next valid match.

  3. The vector indexes rank EMBEDDINGS, not people, and nothing deduped them.
     Harmless while truncation came first; combined with fix (2) it would have
     backfilled freed slots with more copies of the same survivor.

The unit tests drive `_search_indexes` directly with stubbed indexes and a fake
session, because that is where the ordering lives and there is no DB fixture
factory in this suite. The live test at the bottom is the end-to-end guard that
would have caught defect (1) originally.
"""

import asyncio
import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import List, Optional

import pytest

from backend.core.advanced_search import AdvancedSearchService, FaceSearchResult

BASE = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

@dataclass
class FakeIdentity:
    """Stands in for a hydrated db_models.Identity row."""
    id: uuid.UUID
    display_name: str
    pipeline_ids: tuple = ()          # which cameras this identity was seen on
    last_seen_at: Optional[object] = None
    best_snapshot_path: Optional[str] = None
    appearances_count: int = 0
    type: Optional[object] = None


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """Applies the filters the real query would, by inspecting what was asked.

    The production code builds one SQLAlchemy Select and adds predicates to it.
    Rather than parse SQL, the service is handed a session that records the
    query and a resolver decides which identities that query admits — the same
    decision Postgres would make, expressed in Python.
    """

    def __init__(self, identities, admits):
        self.identities = identities
        self.admits = admits           # fn(query) -> set of admitted ids
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        allowed = self.admits(query)
        return FakeResult([i for i in self.identities if str(i.id) in allowed])


class FakeVectorIndex:
    """Returns (identity_id, score) rows — one per EMBEDDING, like the real one."""

    def __init__(self, rows):
        self.rows = rows
        self.requested_top_k = []
        self.requested_threshold = []

    async def search_known(self, embedding, top_k, threshold, db=None):
        self.requested_top_k.append(top_k)
        self.requested_threshold.append(threshold)
        return list(self.rows)

    async def search_unknown(self, embedding, top_k, threshold, db=None):
        self.requested_top_k.append(top_k)
        self.requested_threshold.append(threshold)
        return []


def make_service(rows):
    svc = AdvancedSearchService.__new__(AdvancedSearchService)
    svc.use_pgvector = True
    svc.pgvector_index = FakeVectorIndex(rows)
    svc.identity_index = None
    return svc


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def uid(n):
    """Deterministic UUIDs so assertions can name identities."""
    return uuid.UUID(int=n)


# ---------------------------------------------------------------------------
# 1. Dedup — one row per identity, carrying its best score
# ---------------------------------------------------------------------------

def test_multiple_embeddings_of_one_identity_collapse_to_one_match():
    """pgvector selects FROM identity_embeddings with no DISTINCT, so an
    identity with N embeddings occupied N result slots. 6 identities in the
    live database have more than one embedding."""
    rows = []
    for ident in (1, 2, 3):
        for e in range(10):
            rows.append((str(uid(ident)), 0.9 - ident * 0.1 - e * 0.001))

    identities = [FakeIdentity(uid(i), f"person-{i}") for i in (1, 2, 3)]
    svc = make_service(rows)
    db = FakeSession(identities, admits=lambda q: {str(i.id) for i in identities})

    matches, _ = run(svc._search_indexes(
        embedding=None, db=db, scope="known", top_k=10,
        exclude_identity_ids=None, filters=None))

    ids = [m.identity_id for m in matches]
    assert len(ids) == len(set(ids)), f"the same identity appears twice: {ids}"
    assert len(matches) == 3

    # Each keeps its BEST score, not an arbitrary one.
    best = max(s for i, s in rows if i == str(uid(1)))
    assert matches[0].identity_id == str(uid(1))
    assert matches[0].similarity == pytest.approx(round(best, 4))


def test_dedup_happens_before_the_cut_so_top_k_counts_people():
    """top_k is documented as 'max results per face'; without dedup it counted
    embeddings, so 3 people could fill a 10-slot request."""
    rows = [(str(uid(i)), 0.9 - i * 0.01) for i in range(1, 4) for _ in range(10)]
    identities = [FakeIdentity(uid(i), f"p{i}") for i in range(1, 4)]
    svc = make_service(rows)
    db = FakeSession(identities, admits=lambda q: {str(i.id) for i in identities})

    matches, _ = run(svc._search_indexes(
        embedding=None, db=db, scope="known", top_k=10,
        exclude_identity_ids=None, filters=None))
    assert len(matches) == 3, "expected 3 distinct people, got embedding rows"


# ---------------------------------------------------------------------------
# 2. Filter, then rank, then truncate
# ---------------------------------------------------------------------------

def test_filter_promotes_the_next_valid_match_instead_of_shrinking():
    """The bug: truncate to top_k first, then filter, then drop the misses.
    Ask for 5 with a filter that rejects the 35 best candidates and you used to
    get 0 — the survivors were never in the truncated list."""
    rows = [(str(uid(i)), 1.0 - i * 0.01) for i in range(1, 41)]
    identities = [FakeIdentity(uid(i), f"p{i}") for i in range(1, 41)]
    survivors = {str(uid(i)) for i in range(36, 41)}

    svc = make_service(rows)
    db = FakeSession(identities, admits=lambda q: survivors)

    matches, _ = run(svc._search_indexes(
        embedding=None, db=db, scope="known", top_k=5,
        exclude_identity_ids=None,
        filters={"pipeline_id": "cam-a"}))

    assert len(matches) == 5, (
        f"filter shrank the result instead of promoting: got {len(matches)}"
    )
    assert {m.identity_id for m in matches} == survivors
    # Still ranked by score, best first.
    assert [m.similarity for m in matches] == sorted(
        (m.similarity for m in matches), reverse=True)


def test_result_is_capped_at_top_k():
    """Removing the pre-truncation must not remove the cap."""
    rows = [(str(uid(i)), 1.0 - i * 0.01) for i in range(1, 41)]
    identities = [FakeIdentity(uid(i), f"p{i}") for i in range(1, 41)]
    svc = make_service(rows)
    db = FakeSession(identities, admits=lambda q: {str(i.id) for i in identities})

    matches, _ = run(svc._search_indexes(
        embedding=None, db=db, scope="known", top_k=7,
        exclude_identity_ids=None, filters=None))
    assert len(matches) == 7


def test_no_duplicate_backfill_when_a_filter_empties_the_pool():
    """The failure mode the naive fix would have introduced: with competing
    identities filtered out, 'fill to top_k' walks deeper and finds only more
    embeddings of the one survivor."""
    rows = []
    for ident in (1, 2, 3):
        for e in range(10):
            rows.append((str(uid(ident)), 0.9 - ident * 0.1 - e * 0.001))
    identities = [FakeIdentity(uid(i), f"p{i}") for i in (1, 2, 3)]

    svc = make_service(rows)
    db = FakeSession(identities, admits=lambda q: {str(uid(2))})

    matches, _ = run(svc._search_indexes(
        embedding=None, db=db, scope="known", top_k=10,
        exclude_identity_ids=None, filters={"pipeline_id": "cam-a"}))

    assert len(matches) == 1, (
        f"backfilled the freed slots with duplicates: {[m.identity_id for m in matches]}"
    )
    assert matches[0].identity_id == str(uid(2))


def test_excluded_identities_are_never_backfilled():
    """exclude_identity_ids is an explicit 'do not show me this person'. The
    fill loop must not reintroduce them from deeper in the candidate list."""
    rows = [(str(uid(i)), 1.0 - i * 0.01) for i in range(1, 11)]
    identities = [FakeIdentity(uid(i), f"p{i}") for i in range(1, 11)]
    excluded = {str(uid(3)), str(uid(4))}

    svc = make_service(rows)
    db = FakeSession(identities, admits=lambda q: {str(i.id) for i in identities})

    matches, _ = run(svc._search_indexes(
        embedding=None, db=db, scope="known", top_k=10,
        exclude_identity_ids=excluded, filters=None))

    assert not (excluded & {m.identity_id for m in matches})


# ---------------------------------------------------------------------------
# 3. Alert scoping — the security-relevant one
# ---------------------------------------------------------------------------

def test_alert_candidates_ignore_the_filters():
    """Watchlist alerts become persisted WatchlistAlert rows and feed
    SearchHistory.watchlist_alerts_count. If they were derived from the
    filtered matches, narrowing the Camera dropdown would silently suppress a
    threat alert AND its audit trail."""
    rows = [(str(uid(i)), 1.0 - i * 0.01) for i in range(1, 6)]
    identities = [FakeIdentity(uid(i), f"p{i}") for i in range(1, 6)]
    only_one = {str(uid(5))}

    svc = make_service(rows)
    db = FakeSession(identities, admits=lambda q: (
        only_one if _query_is_filtered(q) else {str(i.id) for i in identities}))

    matches, alert_candidates = run(svc._search_indexes(
        embedding=None, db=db, scope="known", top_k=5,
        exclude_identity_ids=None, filters={"pipeline_id": "cam-a"}))

    assert {m.identity_id for m in matches} == only_one, "filter was not applied"
    assert len(alert_candidates) == 5, (
        "alert candidates were narrowed by a browse filter — a camera filter "
        "must not decide whether a threat alert is recorded"
    )


def test_alert_candidates_are_the_same_list_when_no_filter_is_active():
    """No filter means no second query and no divergence."""
    rows = [(str(uid(i)), 1.0 - i * 0.01) for i in range(1, 4)]
    identities = [FakeIdentity(uid(i), f"p{i}") for i in range(1, 4)]
    svc = make_service(rows)
    db = FakeSession(identities, admits=lambda q: {str(i.id) for i in identities})

    matches, alert_candidates = run(svc._search_indexes(
        embedding=None, db=db, scope="known", top_k=5,
        exclude_identity_ids=None, filters=None))

    assert matches is alert_candidates
    assert len(db.queries) == 1, "an unnecessary second query was issued"


def _query_is_filtered(query):
    """True when the query carries a date or camera predicate.

    Inspects the WHERE clause, not the whole statement: `select(Identity)`
    expands to every column, so `last_seen_at` appears in the SELECT list of
    the *unfiltered* query too and a naive substring check calls everything
    filtered.
    """
    # Explicit None check: a SQLAlchemy clause raises on truthiness testing,
    # so `whereclause or ""` is a TypeError, not a default.
    clause = query.whereclause
    where = "" if clause is None else str(clause).lower()
    return "exists" in where or "last_seen_at" in where


# ---------------------------------------------------------------------------
# 4. Candidate pool sizing
# ---------------------------------------------------------------------------

def test_pool_widens_when_a_filter_is_active():
    """Filtering happens after the index has already chosen candidates, so a
    filtered search has to start from a wider pool or it under-fills."""
    rows = [(str(uid(1)), 0.9)]
    identities = [FakeIdentity(uid(1), "p1")]

    svc = make_service(rows)
    db = FakeSession(identities, admits=lambda q: {str(uid(1))})
    run(svc._search_indexes(embedding=None, db=db, scope="known", top_k=10,
                            exclude_identity_ids=None, filters=None))
    unfiltered_k = svc.pgvector_index.requested_top_k[0]

    svc2 = make_service(rows)
    db2 = FakeSession(identities, admits=lambda q: {str(uid(1))})
    run(svc2._search_indexes(embedding=None, db=db2, scope="known", top_k=10,
                             exclude_identity_ids=None,
                             filters={"pipeline_id": "cam-a"}))
    filtered_k = svc2.pgvector_index.requested_top_k[0]

    assert filtered_k > unfiltered_k, (
        f"filtered search asked for {filtered_k}, unfiltered for {unfiltered_k}"
    )


def test_both_backends_search_to_the_same_depth():
    """pgvector was called with threshold=0.2 under a comment saying 'lower
    threshold for advanced search'; the FAISS calls passed no threshold at all
    and silently used the index defaults (0.4 known / 0.35 unknown), so the two
    deployments searched to different depths."""
    import inspect
    src = inspect.getsource(AdvancedSearchService._search_indexes)
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#"))

    assert "threshold=self.candidate_threshold" in code
    # Every search call passes it — none fall back to a default.
    #
    # The call shape changed with the vector-index contract: the in-process
    # branch no longer calls search_known()/search_unknown() on a two-index
    # service, it calls search_similar_embeddings() once per scope and lets
    # PostgreSQL decide which hits are KNOWN and which are UNKNOWN. The
    # invariant under test is unchanged — both backends search to the same
    # depth — so count the search calls that actually exist.
    calls = (code.count("search_known(") + code.count("search_unknown(")
             + code.count("search_similar_embeddings("))
    passes = code.count("threshold=self.candidate_threshold")
    assert calls > 0, "no search calls found — the test is no longer looking at the right code"
    assert passes == calls, (
        f"{calls} search calls but only {passes} pass an explicit threshold"
    )


def test_ef_search_never_drops_below_the_requested_k():
    """pgvector needs ef_search >= k; a widened pool with a fixed ef_search
    silently loses recall instead of erroring."""
    from backend.core.identity_index_pgvector import IdentityIndexPgVector

    index = IdentityIndexPgVector.__new__(IdentityIndexPgVector)
    index.hnsw_ef_search = 100
    assert index._effective_ef_search(20) == 100, "configured value is a floor"
    assert index._effective_ef_search(600) == 600, "must rise to meet k"
    assert index._effective_ef_search(None) == 100
    index.hnsw_ef_search = None
    assert index._effective_ef_search(40) == 40


# ---------------------------------------------------------------------------
# 5. Live end-to-end guard
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def token():
    req = urllib.request.Request(
        BASE + "/api/auth/login",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["access_token"]


def _get(path, token):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _search(token, image_bytes, **fields):
    boundary = uuid.uuid4().hex
    parts = [("--" + boundary).encode(),
             b'Content-Disposition: form-data; name="image"; filename="p.jpg"',
             b"Content-Type: image/jpeg", b"", image_bytes]
    for name, value in fields.items():
        parts += [("--" + boundary).encode(),
                  ('Content-Disposition: form-data; name="%s"' % name).encode(),
                  b"", str(value).encode()]
    parts.append(("--" + boundary + "--").encode())

    req = urllib.request.Request(BASE + "/api/search/advanced",
                                 data=b"\r\n".join(parts), method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _match_ids(result):
    return [m["identity_id"]
            for f in result.get("faces", [])
            for m in f.get("matches", [])]


def test_camera_filter_changes_the_result_set(token):
    """The assertion that would have caught the original dead-parameter bug:
    before the fix this returned the IDENTICAL set for any camera."""
    identities = _get("/api/admin/identities?limit=20&type=known", token).get("identities", [])
    snapshot = next((i["snapshot_url"] for i in identities
                     if (i.get("snapshot_url") or "").startswith("/")), None)
    if not snapshot:
        pytest.skip("no stored snapshot to search with")

    req = urllib.request.Request(BASE + snapshot)
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        image = resp.read()

    baseline = _match_ids(_search(token, image, scope="both", top_k=10,
                                  check_watchlist="false"))
    if not baseline:
        pytest.skip("probe image returned no matches")

    # A camera id that cannot have appearances.
    filtered = _match_ids(_search(token, image, scope="both", top_k=10,
                                  check_watchlist="false",
                                  pipeline_id="pytest-no-such-camera"))

    assert filtered != baseline or not baseline, (
        "pipeline_id had no effect on the result set — the filter is a no-op again"
    )
    assert len(filtered) <= len(baseline)


def test_no_identity_is_repeated_in_a_single_face_result(token):
    """Live shape check: the vector query ranks embeddings, so without dedup one
    person can occupy several slots of the same face's match list."""
    identities = _get("/api/admin/identities?limit=20&type=known", token).get("identities", [])
    snapshot = next((i["snapshot_url"] for i in identities
                     if (i.get("snapshot_url") or "").startswith("/")), None)
    if not snapshot:
        pytest.skip("no stored snapshot to search with")

    req = urllib.request.Request(BASE + snapshot)
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        image = resp.read()

    result = _search(token, image, scope="both", top_k=50, check_watchlist="false")
    for face in result.get("faces", []):
        ids = [m["identity_id"] for m in face.get("matches", [])]
        assert len(ids) == len(set(ids)), f"identity repeated in one face: {ids}"


def test_search_still_works_end_to_end(token):
    """The reordering touches the hot path of every search; this is the
    smoke test that the response shape is unchanged."""
    identities = _get("/api/admin/identities?limit=20&type=known", token).get("identities", [])
    snapshot = next((i["snapshot_url"] for i in identities
                     if (i.get("snapshot_url") or "").startswith("/")), None)
    if not snapshot:
        pytest.skip("no stored snapshot to search with")

    req = urllib.request.Request(BASE + snapshot)
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        image = resp.read()

    result = _search(token, image, scope="both", top_k=5, check_watchlist="true")
    for key in ("search_id", "faces", "watchlist_alerts", "summary", "processing_time_ms"):
        assert key in result, f"response lost {key}"
    assert isinstance(result["watchlist_alerts"], list)
    summary = result["summary"]
    for key in ("total_faces_detected", "total_matches", "unique_identities_found"):
        assert key in summary
