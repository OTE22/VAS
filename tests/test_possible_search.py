"""Separate lower-score review candidates using synthetic, isolated evidence."""
from datetime import datetime
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import uuid

import numpy as np
import pytest

from config import settings
import db_models as m
from conftest import run_on_shared_loop as run
from test_person_history_retention import isolated
from test_search_filters import MODEL, vector, person, sighting, enrollment
from backend.core import advanced_search as module
from backend.core.search_evidence import search_evidence


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setattr(settings, "SIMILARITY_THRESHOLD", .6)
    monkeypatch.setattr(settings, "SEARCH_POSSIBLE_MIN_SIMILARITY", .5)
    monkeypatch.setattr(settings, "RECOGNITION_MODEL", MODEL + ".onnx")


def angle(score):
    return float(np.rad2deg(np.arccos(score)))


def test_possible_band_includes_floor_excludes_strong_boundary(isolated):
    sessions, _ = isolated

    async def check():
        async with sessions() as db:
            identities = {}
            for score in (.49, .5, .55, .6, .7):
                candidate = await person(db)
                identities[score] = str(candidate.id)
                await sighting(db, candidate, angle(score))
            service = module.AdvancedSearchService()
            strong, alerts = await service._search_indexes(vector(0), db, "both", 10, [], {})
            possible = await service._search_possible_matches(vector(0), db, "both", 10, [], {})
            assert {item.identity_id for item in strong} == {identities[.6], identities[.7]}
            assert [item.identity_id for item in alerts] == [item.identity_id for item in strong]
            assert [item.identity_id for item in possible] == [identities[.55], identities[.5]]
            assert all(item.match_tier == "possible" and item.watchlist_match is None for item in possible)
            assert all(item.match_tier == "strong" for item in strong)

    run(check())


def test_possible_limit_is_independent_and_strong_groups_cannot_reappear(isolated):
    sessions, _ = isolated

    async def check():
        async with sessions() as db:
            strong_people = []
            for score in (.95, .9, .85, .8):
                candidate = await person(db)
                strong_people.append(str(candidate.id))
                await sighting(db, candidate, angle(score))
                # Filtering the upper bound before ranking would incorrectly
                # turn this weaker observation into a second candidate.
                await sighting(db, candidate, angle(.59), day=2)
            possible_people = []
            for score in (.58, .56, .54):
                candidate = await person(db)
                possible_people.append(str(candidate.id))
                for day in (1, 2, 3):
                    await sighting(db, candidate, angle(score), day=day)
            service = module.AdvancedSearchService()
            strong, _ = await service._search_indexes(vector(0), db, "both", 2, [], {})
            possible = await service._search_possible_matches(vector(0), db, "both", 2, [], {})
            assert [item.identity_id for item in strong] == strong_people[:2]
            assert [item.identity_id for item in possible] == possible_people[:2]
            assert not {item.identity_id for item in possible}.intersection(strong_people)

    run(check())


def test_possible_known_and_unknown_keep_exact_photo_but_never_name_or_verification(isolated):
    sessions, _ = isolated

    async def check():
        async with sessions() as db:
            known = await person(db, m.IdentityType.KNOWN)
            unknown = await person(db)
            await enrollment(db, known, 30)
            await sighting(db, unknown, 30, camera="anchor")
            expected = {}
            for candidate in (known, unknown):
                face, app, embedding = await sighting(db, candidate, angle(.55), camera="review")
                expected[str(candidate.id)] = (face, app, embedding)
            # This Known group has a reference that agrees strongly with both
            # the query and the weaker sighting. The review-only result must
            # still avoid presenting the enrolled name.
            raw = await search_evidence(
                db, vector(0), MODEL, "known", 10, pipeline_id="review",
                min_similarity=.5, max_similarity=.6)
            assert raw[0].verification == "verified_reference"
            possible = await module.AdvancedSearchService()._search_possible_matches(
                vector(0), db, "both", 10, [], {"pipeline_id": "review"})
            assert len(possible) == 2
            assert {item.type for item in possible} == {"unknown", "unverified"}
            for item in possible:
                face, app, embedding = expected[item.identity_id]
                assert item.display_name is None and item.reference_similarity is None
                assert item.identity_verification == "unverified" and item.watchlist_match is None
                assert item.confidence_band == "POSSIBLE"
                assert item.best_snapshot_path == face.face_image_path
                assert (item.matched_embedding_id, item.matched_appearance_id, item.matched_face_id) == (
                    embedding.id, app.id, face.id)

    run(check())


def test_possible_candidates_preserve_scope_dates_camera_and_exclusions(isolated):
    sessions, _ = isolated

    async def check():
        async with sessions() as db:
            known = await person(db, m.IdentityType.KNOWN)
            unknown, excluded = await person(db), await person(db)
            _, selected, embedding = await sighting(db, unknown, angle(.55), camera="north", day=2)
            await sighting(db, unknown, angle(.58), camera="south", day=3)
            await sighting(db, known, angle(.56), camera="north", day=2)
            await sighting(db, excluded, angle(.57), camera="north", day=2)
            service = module.AdvancedSearchService()
            filters = {"pipeline_id": "north", "date_from": datetime(2026, 1, 2),
                       "date_to": datetime(2026, 1, 2, 23, 59, 59)}
            hits = await service._search_possible_matches(
                vector(0), db, "unknown", 10, [str(excluded.id)], filters)
            assert len(hits) == 1 and hits[0].identity_id == str(unknown.id)
            assert hits[0].matched_embedding_id == embedding.id
            assert hits[0].matched_appearance_id == selected.id
            hits = await service._search_possible_matches(vector(0), db, "known", 10, [], filters)
            assert len(hits) == 1 and hits[0].identity_id == str(known.id)
            filters["date_from"] = datetime(2026, 1, 3)
            assert not await service._search_possible_matches(vector(0), db, "both", 10, [], filters)

    run(check())


def test_lower_candidate_never_enters_watchlist_checks(monkeypatch):
    service = module.AdvancedSearchService()
    strong = module.FaceSearchResult(str(uuid.uuid4()), "Enrolled", "known", .9, "HIGH",
                                     identity_verification="verified_reference")
    possible = module.FaceSearchResult(str(uuid.uuid4()), None, "unknown", .55, "POSSIBLE",
                                       match_tier="possible")
    service._search_indexes = AsyncMock(return_value=([strong], [strong]))
    service._search_possible_matches = AsyncMock(return_value=[possible])
    service._check_watchlists = AsyncMock(return_value=[strong])
    monkeypatch.setattr(module, "assess_face_quality", lambda **kwargs: NS(
        overall_score=.9, details={}, warnings=[]))
    monkeypatch.setattr(module, "embed_face_normalized", lambda *args, **kwargs: vector(0))
    face = NS(bbox_int=lambda shape: (0, 0, 16, 16), landmarks=np.zeros((5, 2)))
    result = run(service._process_single_face(
        np.zeros((16, 16, 3), dtype=np.uint8), face, 0, object(), "both", 10, .1,
        True, [], [], {}))
    assert service._check_watchlists.await_count == 1
    assert service._check_watchlists.call_args.kwargs["matches"] == [strong]
    assert result.alert_candidates == [strong] and result.matches == [strong]
    assert result.possible_matches == [possible]


def test_possible_only_search_serializes_without_claiming_strong_matches(monkeypatch):
    from backend.routes.advanced_search import MultiSearchResponse
    from backend.core import search_audit

    service = module.AdvancedSearchService(model_manager=object())
    service._initialized = True
    possible = module.FaceSearchResult(str(uuid.uuid4()), None, "unknown", .55, "POSSIBLE",
        match_tier="possible", matched_embedding_id=12, matched_appearance_id=34)
    face = module.FaceInImage(0, {}, .9, {}, possible_matches=[possible], embedding=vector(0))
    service._process_single_face = AsyncMock(return_value=face)
    monkeypatch.setattr(module, "extract_faces", lambda *args, **kwargs: [
        NS(index=0, padded_retry=False)])
    audit = AsyncMock()
    monkeypatch.setattr(search_audit, "record_image_search", audit)
    result = run(service.search_multi_face(
        np.zeros((16, 16, 3), dtype=np.uint8), object(), 7, image_hash="synthetic"))
    payload = MultiSearchResponse(**service.to_dict(result)).model_dump()
    assert payload["faces"][0]["matches"] == [] and payload["watchlist_alerts"] == []
    hit = payload["faces"][0]["possible_matches"][0]
    assert hit["matched_embedding_id"] == 12 and hit["matched_appearance_id"] == 34
    assert hit["match_tier"] == "possible" and hit["display_name"] is None
    assert payload["summary"]["total_matches"] == 0
    assert payload["summary"]["possible_matches"] == 1
    assert audit.call_args.kwargs["results_count"] == 0
    assert audit.call_args.kwargs["watchlist_alerts_count"] == 0
    assert payload["thresholds"] == {"strong": .6, "possible": .5}


def test_batch_keeps_possible_provenance_separate_from_strong_totals(monkeypatch):
    from backend.core.batch_search_service import BatchSearchService
    from backend.routes.batch_export import BatchSearchResponse
    from db_connection import db_manager

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(db_manager, "get_session", session)
    candidate = module.FaceSearchResult(str(uuid.uuid4()), None, "unknown", .55, "POSSIBLE",
        match_tier="possible", matched_embedding_id=12, matched_appearance_id=34,
        snapshot_url="/storage/exact-candidate.jpg")
    bounding_box = {"x1": 4, "y1": 6, "x2": 14, "y2": 16}
    face = module.FaceInImage(2, bounding_box, .9, {}, possible_matches=[candidate])
    advanced = module.AdvancedSearchService()
    advanced.search_multi_face = AsyncMock(return_value=module.MultiSearchResult(
        "test", {}, [face], [], 1,
        {"total_faces_detected": 1, "faces_searchable": 1, "watchlist_alerts": 0}))
    batch = BatchSearchService()
    batch.initialize(advanced)
    batch._log_batch_history = AsyncMock()
    result = run(batch.search_batch(
        object(), [("reference.png", np.zeros((16, 16, 3), dtype=np.uint8), "synthetic")], 7))
    payload = BatchSearchResponse(**batch.to_dict(result)).model_dump()
    assert payload["successful_images"] == 1 and payload["failed_images"] == 0
    assert payload["total_matches"] == payload["unique_identities"] == payload["watchlist_alerts"] == 0
    assert payload["possible_matches"] == 1
    group = payload["results"][0]
    assert group["matches"] == []
    hit = group["possible_matches"][0]
    assert hit["face_index"] == 2 and hit["match_tier"] == "possible"
    assert hit["query_bounding_box"] == bounding_box
    assert hit["matched_embedding_id"] == 12 and hit["matched_appearance_id"] == 34
    assert hit["snapshot_url"] == "/storage/exact-candidate.jpg" and hit["display_name"] is None
    assert batch._log_batch_history.call_args.kwargs["results_count"] == 0
    assert payload["thresholds"] == {"strong": .6, "possible": .5}


@pytest.mark.parametrize("floor", [.6, .7])
def test_possible_band_disabled_when_floor_is_not_lower(monkeypatch, floor):
    monkeypatch.setattr(settings, "SEARCH_POSSIBLE_MIN_SIMILARITY", floor)
    db = NS(execute=AsyncMock(side_effect=AssertionError("Disabled band must not query")))
    assert run(module.AdvancedSearchService()._search_possible_matches(
        vector(0), db, "both", 10, [], {})) == []
