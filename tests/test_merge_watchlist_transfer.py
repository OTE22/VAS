"""
Merge re-points watchlist membership and live alerts (plan §4).

    * a loser's watchlist entry moves to the winner when the winner is not on
      that list — the entry row keeps its id, so its historical alerts follow;
    * when the winner already has the pair the loser's row is RETIRED IN PLACE
      (is_active=false, never deleted — deleting would cascade its alerts);
      the winner's row absorbs the higher priority / later expiry;
    * live search alerts move to the winner;
    * unmerge restores membership (moved back / re-activated, winner restored)
      from the recorded provenance and refuses when the loser regained the pair;
    * the approve-suggestion path uses the same merge function.

    docker exec face_recognition_api python -m pytest tests/test_merge_watchlist_transfer.py -q
"""
import os
from datetime import datetime

import pytest

from test_unmerge import (_http, _sql, _enroll, _make_unknown, _add_embedding, _add_face,
                          _merge, _unmerge, _merge_id, _provenance, _reason,
                          _cleanup_prefix, FACE_B, QA_PIPELINE)
from conftest import run_on_shared_loop as run_async

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PFX = "qa_unmrg_wl_"          # inside test_unmerge's cleanup prefix (qa_unmrg_)
LIST_PFX = "qa_wl_merge_"


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login", body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


def _cleanup_lists():
    _sql("DELETE FROM watchlists WHERE name LIKE :p", {"p": LIST_PFX + "%"})   # entries+alerts cascade
    _sql("DELETE FROM live_search_alerts WHERE name LIKE :p", {"p": LIST_PFX + "%"})


@pytest.fixture(autouse=True)
def _clean():
    _cleanup_lists()
    _cleanup_prefix()
    yield
    _cleanup_lists()
    _cleanup_prefix()


def _new_list(name):
    return str(_sql(
        "INSERT INTO watchlists (id, name, alert_level, notify_dashboard, notify_email, notify_sms, notify_webhook, "
        " is_active, created_at, version) VALUES (gen_random_uuid(), :n, 'WARNING', true, false, false, false, "
        " true, now(), 1) RETURNING id", {"n": LIST_PFX + name}, fetch="scalar"))


def _add_entry(list_id, identity_id, priority="NORMAL", active=True, expires=None):
    return str(_sql(
        "INSERT INTO watchlist_entries (id, watchlist_id, identity_id, priority, added_at, is_active, expires_at) "
        "VALUES (gen_random_uuid(), CAST(:l AS uuid), CAST(:i AS uuid), CAST(:p AS watchlistentrypriority), now(), :a, "
        " CAST(:e AS timestamp)) RETURNING id",
        {"l": list_id, "i": identity_id, "p": priority, "a": active, "e": expires}, fetch="scalar"))


def _add_alert(entry_id):
    return str(_sql(
        "INSERT INTO watchlist_alerts (id, watchlist_entry_id, triggered_by, pipeline_id, acknowledged, created_at) "
        "VALUES (gen_random_uuid(), CAST(:e AS uuid), 'detection', :p, false, now()) RETURNING id",
        {"e": entry_id, "p": QA_PIPELINE}, fetch="scalar"))


def _add_live_alert(identity_id, name):
    return str(_sql(
        "INSERT INTO live_search_alerts (id, name, identity_id, min_similarity, time_window_enabled, cooldown_minutes, "
        " notify_dashboard, notify_email, notify_sms, notify_webhook, sound_alert, auto_capture_snapshot, auto_record_clip, "
        " clip_duration_seconds, expiration_type, status, triggers_count, created_at) "
        "VALUES (gen_random_uuid(), :n, CAST(:i AS uuid), 0.6, false, 5, true, false, false, false, true, false, false, "
        " 0, 'NEVER', 'ACTIVE', 0, now()) "
        "RETURNING id", {"n": LIST_PFX + name, "i": identity_id}, fetch="scalar"))


def _entry(entry_id):
    row = _sql("SELECT identity_id::text, is_active, priority::text, expires_at FROM watchlist_entries "
               "WHERE id = CAST(:e AS uuid)", {"e": entry_id})
    return row[0] if row else None


def _matches_for(identity_id):
    from backend.core.watchlist_service import watchlist_service
    from db_connection import db_manager

    async def _run():
        async with db_manager.get_session() as db:
            found = await watchlist_service.check_identities_against_watchlists(db, [identity_id])
            return list(found.get(identity_id, []))    # {identity_id: [match, ...]}
    return run_async(_run())


def _pair(token, tag):
    winner = _enroll(token, PFX + tag + "_winner", FACE_B)
    loser = _make_unknown(PFX + tag + "_loser")
    _add_embedding(loser)
    _add_face(loser)
    return loser, winner


# ---------------------------------------------------------------- transfer

def test_merge_moves_membership_and_live_alerts_to_the_winner(token):
    loser, winner = _pair(token, "move")
    l1 = _new_list("move")
    entry = _add_entry(l1, loser, "HIGH")
    hist = _add_alert(entry)
    live = _add_live_alert(loser, "move_live")

    status, body = _merge(token, loser, winner)
    assert status == 200, body

    row = _entry(entry)
    assert row[0] == winner and row[1] is True and row[2] == "HIGH", row      # same row, re-pointed
    assert _sql("SELECT watchlist_entry_id::text FROM watchlist_alerts WHERE id = CAST(:a AS uuid)",
                {"a": hist}, fetch="scalar") == entry                          # history follows the entry id
    assert _sql("SELECT identity_id::text FROM live_search_alerts WHERE id = CAST(:a AS uuid)",
                {"a": live}, fetch="scalar") == winner
    prov = _provenance(_merge_id(loser))
    moves = prov["watchlist_entry_moves"]
    assert [m["action"] for m in moves] == ["moved"] and moves[0]["entry_id"] == entry, moves
    assert prov["live_alert_ids"] == [live]
    # runtime: the winner matches on the list, the loser matches nothing
    winner_matches = _matches_for(winner)
    assert [m["entry_id"] for m in winner_matches] == [entry], winner_matches
    assert _matches_for(loser) == []


def test_merge_dedupe_retires_the_loser_row_in_place_and_keeps_its_alerts(token):
    loser, winner = _pair(token, "dedupe")
    l2 = _new_list("dedupe")
    winner_entry = _add_entry(l2, winner, "NORMAL", expires=datetime(2030, 1, 1))
    loser_entry = _add_entry(l2, loser, "CRITICAL")            # higher priority, no expiry
    hist = _add_alert(loser_entry)

    status, body = _merge(token, loser, winner)
    assert status == 200, body

    lrow, wrow = _entry(loser_entry), _entry(winner_entry)
    assert lrow is not None and lrow[0] == loser and lrow[1] is False, lrow      # retired in place, still on loser
    assert wrow[0] == winner and wrow[1] is True and wrow[2] == "CRITICAL" and wrow[3] is None, wrow  # absorbed
    assert _sql("SELECT count(*) FROM watchlist_alerts WHERE id = CAST(:a AS uuid) AND watchlist_entry_id = CAST(:e AS uuid)",
                {"a": hist, "e": loser_entry}, fetch="scalar") == 1               # alert survives, still resolves
    prov = _provenance(_merge_id(loser))
    m = prov["watchlist_entry_moves"][0]
    assert m["action"] == "retired_duplicate" and m["winner_entry_id"] == winner_entry
    assert m["before"]["is_active"] is True and m["winner_before"]["priority"] == "normal"   # enum .value
    # only the winner's membership matches at runtime; exactly one match, not two
    matches = _matches_for(winner)
    assert [x["entry_id"] for x in matches] == [winner_entry], matches
    assert _matches_for(loser) == []
    # data quality: no active entry on a MERGED identity
    assert _sql("SELECT count(*) FROM watchlist_entries e JOIN identities i ON i.id = e.identity_id "
                "WHERE i.status = 'MERGED' AND e.is_active", fetch="scalar") == 0


# ---------------------------------------------------------------- unmerge

def test_unmerge_restores_moved_and_retired_membership_and_live_alerts(token):
    loser, winner = _pair(token, "restore")
    l1, l2 = _new_list("restore_a"), _new_list("restore_b")
    moved = _add_entry(l1, loser, "LOW")
    winner_entry = _add_entry(l2, winner, "NORMAL", expires=datetime(2030, 1, 1))
    retired = _add_entry(l2, loser, "HIGH")
    live = _add_live_alert(loser, "restore_live")
    status, body = _merge(token, loser, winner)
    assert status == 200, body
    assert _entry(moved)[0] == winner and _entry(retired)[1] is False and _entry(winner_entry)[2] == "HIGH"

    status, body = _unmerge(token, _merge_id(loser))
    assert status == 200, body

    assert _entry(moved)[0] == loser and _entry(moved)[1] is True
    r = _entry(retired)
    assert r[0] == loser and r[1] is True and r[2] == "HIGH", r
    w = _entry(winner_entry)
    assert w[2] == "NORMAL" and w[3] is not None, w                              # winner restored from provenance
    assert _sql("SELECT identity_id::text FROM live_search_alerts WHERE id = CAST(:a AS uuid)",
                {"a": live}, fetch="scalar") == loser


def test_unmerge_refuses_when_the_loser_regained_the_pair(token):
    loser, winner = _pair(token, "conflict")
    l1 = _new_list("conflict")
    moved = _add_entry(l1, loser)
    status, body = _merge(token, loser, winner)
    assert status == 200, body
    # the loser is independently re-added to the same list after the merge
    clash = _add_entry(l1, loser)
    status, body = _unmerge(token, _merge_id(loser))
    assert status == 409, (status, body)
    assert _reason(body) == "post_merge_watchlist_conflict", body
    # nothing moved: the moved entry is still on the winner, the clash entry untouched
    assert _entry(moved)[0] == winner and _entry(clash)[0] == loser
    assert _sql("SELECT status::text FROM identities WHERE id = CAST(:i AS uuid)", {"i": loser}, fetch="scalar") == "MERGED"


# ---------------------------------------------------------------- contracts

def test_every_merge_entry_point_transfers_membership():
    src = open(f"{REPO}/backend/core/identity_service.py", encoding="utf-8").read()
    pair = src.index("    async def merge_identities(")
    multi = src.index("    async def merge_multiple_identities(")
    unmerge = src.index("    async def unmerge_identity(")
    assert "transfer_watchlist_membership(" in src[pair:unmerge]
    assert "transfer_watchlist_membership(" in src[multi:multi + 40000]
    assert "restore_watchlist_membership(" in src[unmerge:multi]
    routes = open(f"{REPO}/backend/routes/identities.py", encoding="utf-8").read()
    approve = routes.index("async def approve_merge_suggestion(")
    assert "identity_service.merge_identities(" in routes[approve:approve + 20000], \
        "approve-suggestion must go through merge_identities (which transfers membership)"
    assert "DELETE FROM watchlist_entries" not in src and ".delete(WatchlistEntry" not in src, \
        "merge never deletes a membership row (its alerts would cascade)"


def test_no_orphan_alert_and_no_active_membership_on_merged_identities():
    assert _sql("SELECT count(*) FROM watchlist_alerts a LEFT JOIN watchlist_entries e ON e.id = a.watchlist_entry_id "
                "WHERE e.id IS NULL", fetch="scalar") == 0
    assert _sql("SELECT count(*) FROM watchlist_entries e JOIN identities i ON i.id = e.identity_id "
                "WHERE i.status = 'MERGED' AND e.is_active", fetch="scalar") == 0
