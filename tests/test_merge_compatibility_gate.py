"""The merge compatibility gate: no destructive merge without evidence or intent.

    docker exec face_recognition_api python -m pytest tests/test_merge_compatibility_gate.py -v

A merge asserts "these are the same person" and cannot be fully undone once
its gallery adoption deletes the loser's originals. Until this gate existed,
nothing checked the claim: any two identities could be combined by one click,
and every non-2xx came back as a generic toast.

The contract pinned here:

  * every merge entry point (pair, multi, preview execution, suggestion
    approval, promote merge-into-known) passes ONE backend assessment before
    any mutation — the gate lives in the service, so a new caller cannot skip
    it without skipping the merge itself;
  * the identity-pair score is the MEDIAN of cross-identity cosine
    similarities, never the max — one contaminated embedding must not let two
    strangers merge silently;
  * a suspicious request answers a structured 409 MERGE_CONFIRMATION_REQUIRED
    with ZERO mutation; overriding requires an explicit second request, is
    re-assessed server-side, and is audited;
  * what cannot be measured is never scored: no embeddings, or no shared
    model version, yields risk_level "unavailable", not a fabricated number.

House pattern: HTTP against the live app, direct SQL for ground truth,
qa_ prefix + module cleanup.
"""

import json
import urllib.error
import urllib.request
import uuid as uuid_module

import numpy as np
import pytest

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
TEST_PREFIX = "qa_gate2_"
QA_PIPELINE = "qa-gate2-cam"
MODEL = "w600k_r50"


def _http(method, path, *, body=None, token=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:                                      # noqa: BLE001
            return exc.code, {"_raw": raw.decode(errors="replace")}


def _sql(statement, params=None, fetch="all"):
    from sqlalchemy import text

    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            result = await db.execute(text(statement), params or {})
            if not result.returns_rows:
                value = result.rowcount
            elif fetch == "scalar":
                value = result.scalar()
            else:
                value = result.all()
            await db.commit()
            return value
    return run_async(_run())


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


# ---------------------------------------------------------------------------
# seeding — identities whose visual relationship is controlled exactly
# ---------------------------------------------------------------------------

def _unit(seed):
    rng = np.random.default_rng(seed)
    vector = rng.standard_normal(512).astype("float32")
    return vector / np.linalg.norm(vector)


def _literal(vector):
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


def _make_identity(name):
    identity_id = str(_sql(
        "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
        " last_seen_at, created_at, updated_at, appearances_count) "
        "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, now(), now(), now(), "
        "        now(), 0) RETURNING id", {"n": name}, fetch="scalar"))
    _sql("INSERT INTO pipelines (pipeline_id, created_at, is_active) "
         "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING",
         {"p": QA_PIPELINE})
    return identity_id


def _add_vector(identity_id, vector, model=MODEL):
    _sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, embedding, "
         " faiss_index_type, vector_index_sync_state, embedding_model_version, "
         " created_at) "
         "VALUES (:i, :p, CAST(:v AS vector), 'unknown', 'pending', :m, now())",
         {"i": identity_id, "p": QA_PIPELINE, "v": _literal(vector), "m": model})


def _add_raw_vector(identity_id, literal, model=MODEL):
    """Insert an arbitrary vector literal, bypassing app-side validation —
    exactly how corrupt data enters a real system."""
    _sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, embedding, "
         " faiss_index_type, vector_index_sync_state, embedding_model_version, "
         " created_at) "
         "VALUES (:i, :p, CAST(:v AS vector), 'unknown', 'pending', :m, now())",
         {"i": identity_id, "p": QA_PIPELINE, "v": literal, "m": model})


def _status_of(identity_id):
    return _sql("SELECT status::text FROM identities WHERE id = :i",
                {"i": identity_id}, fetch="scalar")


def _merge(token, from_id, to_id, *, confirm=False):
    return _http("POST", "/api/admin/identities/merge", token=token,
                 body={"from_identity_id": from_id, "to_identity_id": to_id,
                       "notes": "qa_gate2", "decision": "merge_existing",
                       "confirm_merge_risk": confirm})


def _cleanup():
    for (identity_id,) in _sql(
            "SELECT id FROM identities WHERE display_name LIKE :p",
            {"p": TEST_PREFIX + "%"}):
        for statement in (
            "UPDATE identities SET merged_into_id = NULL WHERE merged_into_id = :i",
            "DELETE FROM identity_merges WHERE from_identity_id = :i OR to_identity_id = :i",
            "DELETE FROM identity_audit_log WHERE identity_id = :i OR related_identity_id = :i",
            "DELETE FROM identity_embeddings WHERE identity_id = :i",
            "DELETE FROM identity_images WHERE identity_id = :i",
            "DELETE FROM identity_appearances WHERE identity_id = :i",
            "DELETE FROM identities WHERE id = :i",
        ):
            _sql(statement, {"i": str(identity_id)})
    _sql("DELETE FROM merge_suggestions WHERE cluster_id = :c", {"c": TEST_PREFIX})
    _sql("DELETE FROM identity_appearances WHERE pipeline_id = :p", {"p": QA_PIPELINE})
    _sql("DELETE FROM pipelines WHERE pipeline_id = :p", {"p": QA_PIPELINE})


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


SAME = _unit(1)          # "the same person"
OTHER = _unit(2)         # somebody else entirely (random unit vectors ~ 0.0)


# ---------------------------------------------------------------------------
# 1. the happy path is untouched
# ---------------------------------------------------------------------------

def test_compatible_identities_merge_without_ceremony(token):
    a = _make_identity(TEST_PREFIX + "same_a")
    b = _make_identity(TEST_PREFIX + "same_b")
    _add_vector(a, SAME)
    _add_vector(b, SAME)

    status, body = _merge(token, a, b)
    assert status == 200, body
    assert _status_of(a) == "MERGED"


# ---------------------------------------------------------------------------
# 2. suspicious pairs are refused with zero mutation
# ---------------------------------------------------------------------------

def test_low_similarity_returns_409_and_mutates_nothing(token):
    a = _make_identity(TEST_PREFIX + "low_a")
    b = _make_identity(TEST_PREFIX + "low_b")
    _add_vector(a, SAME)
    _add_vector(b, OTHER)

    status, body = _merge(token, a, b)
    assert status == 409, body
    assert body.get("code") == "MERGE_CONFIRMATION_REQUIRED", body
    risk = body.get("risk") or {}
    assert risk.get("risk_level") == "high_risk", risk
    assert risk.get("robust_similarity") is not None
    assert risk["robust_similarity"] < risk["threshold"]
    assert "below_threshold" in risk.get("reason_codes", [])
    assert risk.get("compared_embedding_count", 0) >= 1

    assert _status_of(a) == "ACTIVE", "the refused merge still mutated the loser"
    assert _sql("SELECT count(*) FROM identity_merges WHERE from_identity_id = :i",
                {"i": a}, fetch="scalar") == 0, "a merge record was written"


def test_one_contaminated_embedding_cannot_vouch_for_two_strangers(token):
    """The max cross-pair similarity is 1.0 here; the MEDIAN is ~0. A gate
    built on max would wave this straight through."""
    a = _make_identity(TEST_PREFIX + "cont_a")
    b = _make_identity(TEST_PREFIX + "cont_b")
    _add_vector(a, SAME)
    _add_vector(b, SAME)                      # the contaminated frame
    for seed in (10, 11, 12, 13, 14):
        _add_vector(b, _unit(seed))           # who identity B actually is

    status, body = _merge(token, a, b)
    assert status == 409, (
        "a single mis-attributed embedding made two strangers look mergeable "
        f"-> {status}: {body}")
    assert body["risk"]["robust_similarity"] < body["risk"]["threshold"]


# ---------------------------------------------------------------------------
# 3. override: explicit, re-checked, audited, and effective
# ---------------------------------------------------------------------------

def test_override_recalculates_merges_and_audits(token):
    a = _make_identity(TEST_PREFIX + "ovr_a")
    b = _make_identity(TEST_PREFIX + "ovr_b")
    _add_vector(a, SAME)
    _add_vector(b, OTHER)

    status, _body = _merge(token, a, b)
    assert status == 409

    status, body = _merge(token, a, b, confirm=True)
    assert status == 200, body
    assert _status_of(a) == "MERGED"

    rows = _sql(
        "SELECT username, action_details FROM identity_audit_log "
        "WHERE action_type = 'merge_risk_override' "
        "  AND (identity_id = :b OR related_identity_id = :a)",
        {"a": a, "b": b})
    assert rows, "the override left no audit trail"
    username, details = rows[0]
    details = details if isinstance(details, dict) else json.loads(details or "{}")
    assert username == "admin"
    assert details.get("override") is True
    assert details.get("risk_level") in ("high_risk", "unavailable")
    assert details.get("threshold") is not None
    assert details.get("robust_similarity") is not None
    assert set(details.get("identity_ids", [])) == {a, b}
    assert details.get("reason_codes"), "reason codes must be recorded"


def test_a_compatible_merge_writes_no_override_audit(token):
    a = _make_identity(TEST_PREFIX + "clean_a")
    b = _make_identity(TEST_PREFIX + "clean_b")
    _add_vector(a, SAME)
    _add_vector(b, SAME)
    # confirm_merge_risk on a COMPATIBLE pair must not fabricate an override
    status, _body = _merge(token, a, b, confirm=True)
    assert status == 200
    assert _sql("SELECT count(*) FROM identity_audit_log "
                "WHERE action_type = 'merge_risk_override' AND identity_id = :b",
                {"b": b}, fetch="scalar") == 0, (
        "an override was audited for a merge that never needed one")


# ---------------------------------------------------------------------------
# 4. what cannot be measured is never scored
# ---------------------------------------------------------------------------

def test_missing_embeddings_yield_unavailable_not_a_score(token):
    a = _make_identity(TEST_PREFIX + "noemb_a")     # no embeddings at all
    b = _make_identity(TEST_PREFIX + "noemb_b")
    _add_vector(b, SAME)

    status, body = _merge(token, a, b)
    assert status == 409, body
    risk = body["risk"]
    assert risk["risk_level"] == "unavailable"
    assert risk["robust_similarity"] is None, "a score was fabricated"
    assert any(code.startswith("no_valid_embeddings") for code in risk["reason_codes"])
    assert _status_of(a) == "ACTIVE"


def test_different_model_versions_are_never_compared(token):
    a = _make_identity(TEST_PREFIX + "model_a")
    b = _make_identity(TEST_PREFIX + "model_b")
    _add_vector(a, SAME, model="model_x")
    _add_vector(b, SAME, model="model_y")        # identical vector, alien space

    status, body = _merge(token, a, b)
    assert status == 409, body
    risk = body["risk"]
    assert risk["risk_level"] == "unavailable", (
        "cosine similarity across embedding spaces was treated as meaningful")
    assert any(code.startswith("no_common_model_version")
               for code in risk["reason_codes"]), risk


def test_invalid_vectors_do_not_participate(token):
    """A zero vector could previously poison searches; here it must simply not
    count — leaving these two identities with nothing comparable."""
    a = _make_identity(TEST_PREFIX + "zero_a")
    b = _make_identity(TEST_PREFIX + "zero_b")
    zero = "[" + ",".join("0" for _ in range(512)) + "]"
    _add_raw_vector(a, zero)
    _add_vector(b, SAME)

    status, body = _merge(token, a, b)
    assert status == 409, body
    assert body["risk"]["risk_level"] == "unavailable"
    assert any(code.startswith("no_valid_embeddings")
               for code in body["risk"]["reason_codes"])


# ---------------------------------------------------------------------------
# 5. multi-merge: one stranger blocks the batch
# ---------------------------------------------------------------------------

def test_multi_merge_catches_a_single_unrelated_outlier(token):
    a = _make_identity(TEST_PREFIX + "multi_a")
    b = _make_identity(TEST_PREFIX + "multi_b")
    c = _make_identity(TEST_PREFIX + "multi_c")   # the stranger
    _add_vector(a, SAME)
    _add_vector(b, SAME)
    _add_vector(c, OTHER)

    status, body = _http("POST", "/api/admin/identities/merge-multiple",
                         token=token,
                         body={"identity_ids": [a, b, c], "target_identity_id": a})
    assert status == 409, (
        f"two compatible members averaged away the stranger -> {status}: {body}")
    risk = body["risk"]
    incompatible = {frozenset((p["identity_a"], p["identity_b"]))
                    for p in risk["incompatible_pairs"]}
    assert frozenset((a, c)) in incompatible
    assert frozenset((b, c)) in incompatible
    assert frozenset((a, b)) not in incompatible
    assert f"outlier_member:{c}" in risk["reason_codes"], risk
    for identity_id in (a, b, c):
        assert _status_of(identity_id) == "ACTIVE"

    # the explicit override still works for the whole batch
    status, body = _http("POST", "/api/admin/identities/merge-multiple",
                         token=token,
                         body={"identity_ids": [a, b, c], "target_identity_id": a,
                               "confirm_merge_risk": True})
    assert status == 200, body
    assert _status_of(b) == "MERGED" and _status_of(c) == "MERGED"


# ---------------------------------------------------------------------------
# 6. suggestion approval cannot bypass the gate
# ---------------------------------------------------------------------------

def test_suggestion_approval_passes_the_same_gate(token):
    a = _make_identity(TEST_PREFIX + "sugg_a")
    b = _make_identity(TEST_PREFIX + "sugg_b")
    _add_vector(a, SAME)
    _add_vector(b, OTHER)
    suggestion_id = _sql(
        "INSERT INTO merge_suggestions (cluster_id, identity_ids, confidence, "
        " status, created_at) "
        "VALUES (:c, CAST(:ids AS jsonb), 0.9, 'PENDING', now()) RETURNING id",
        {"c": TEST_PREFIX, "ids": json.dumps([a, b])}, fetch="scalar")

    status, body = _http("POST",
                         f"/api/admin/merge-suggestions/{suggestion_id}/approve",
                         token=token)
    assert status == 409, (
        f"a machine-generated suggestion executed a merge the gate refuses "
        f"-> {status}: {body}")
    assert body.get("code") == "MERGE_CONFIRMATION_REQUIRED"
    assert _status_of(b) == "ACTIVE", "the blocked approval still merged"
    assert _sql("SELECT status::text FROM merge_suggestions WHERE id = :s",
                {"s": suggestion_id}, fetch="scalar") == "PENDING", (
        "the suggestion was marked approved despite the refusal")

    # ... and the explicit override completes it, audited.
    status, body = _http("POST",
                         f"/api/admin/merge-suggestions/{suggestion_id}/approve",
                         token=token, body={"confirm_merge_risk": True})
    assert status == 200, body
    assert _status_of(b) == "MERGED"
    assert _sql("SELECT count(*) FROM identity_audit_log "
                "WHERE action_type = 'merge_risk_override' "
                "  AND (identity_id = :a OR related_identity_id = :b)",
                {"a": a, "b": b}, fetch="scalar") >= 1


# ---------------------------------------------------------------------------
# 7. every service merge entry point runs the gate — proven from the source
# ---------------------------------------------------------------------------

def test_both_service_merge_paths_call_the_gate():
    """AST proof: merge_identities and merge_multiple_identities each await
    _gate_merge_compatibility. A new merge path added to the service without
    the gate shows up here as a missing call."""
    import ast

    source = open("/app/backend/core/identity_service.py", encoding="utf-8").read()
    tree = ast.parse(source)
    gated = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (
                "merge_identities", "merge_multiple_identities"):
            for child in ast.walk(node):
                if (isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "_gate_merge_compatibility"):
                    gated.add(node.name)
    assert gated == {"merge_identities", "merge_multiple_identities"}, (
        f"merge paths without the compatibility gate: "
        f"{ {'merge_identities', 'merge_multiple_identities'} - gated }")


def test_the_threshold_is_the_dedicated_merge_setting():
    """The gate reads MERGE_WARNING_MIN_SIMILARITY — its own knob, not a
    silent reuse of a recognition threshold."""
    import ast

    source = open("/app/backend/core/merge_compatibility.py", encoding="utf-8").read()
    names = {node.attr for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Attribute)}
    assert "MERGE_WARNING_MIN_SIMILARITY" in names
    assert "SIMILARITY_THRESHOLD" not in names, (
        "the gate silently reuses the recognition threshold")


# ---------------------------------------------------------------------------
# 8. frontend contract — source-level pins
# ---------------------------------------------------------------------------

JS = "/app/frontend/js/admin-unknown.js"
STACK_JS = "/app/frontend/js/modal-stack.js"


def _source(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_no_native_confirm_remains_in_merge_workflows():
    source = _source(JS)
    assert "if (!confirm(" not in source, "a native confirm() survives"
    assert "window.confirm" not in source
    # the replacements exist
    assert "AppConfirm.confirm(" in source
    assert source.count("confirmMergeRisk") >= 2


def test_the_409_contract_is_handled_not_toasted():
    source = _source(JS)
    assert "MERGE_CONFIRMATION_REQUIRED" in source, (
        "the structured 409 is treated as an ordinary error")
    assert "postMergeWithRiskGate" in source
    assert "'Merge Anyway'" in source
    assert "confirm_merge_risk: true" in source
    # single-flight: at most one override resend, and one submission at a time
    assert "attempt < 2" in source
    assert "mergeSubmitInFlight" in source


def test_the_card_guard_stops_action_clicks_at_the_boundary():
    source = _source(JS)
    start = source.index("function createIdentityCard")
    end = source.index("function applyFilters")
    card = source[start:end]
    guard = card.index("closest('.identity-actions')")
    view = card.index("viewIdentityDetails(identity.id)")
    assert guard < view, (
        "the card listener reaches viewIdentityDetails before filtering out "
        "clicks on the action buttons — one click opens two modals again")


def test_the_pipeline_merge_button_has_exactly_one_owner():
    source = _source(JS)
    assert "Also attach event listener programmatically as backup" not in source
    assert "closest('.pipeline-merge-suggestions-btn')" not in source, (
        "the delegated duplicate listener is back")
    assert 'class="pipeline-merge-suggestions-btn merge-hover-btn"' in source, (
        "the duplicated class= attribute was not consolidated")
    assert 'data-action="openPipelineMergeSuggestions"' in source


def test_appconfirm_is_stack_based_and_injection_safe():
    source = _source(STACK_JS)
    assert "AppConfirm" in source
    assert "textContent" in source
    assert "innerHTML" not in source, (
        "AppConfirm interpolates caller text into markup")
