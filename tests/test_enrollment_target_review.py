"""Isolated regression tests: no database writes, models, or HTTP server needed."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.core import enrollment_service as service
from backend.routes import upload
from backend.routes import enrollment_review as review


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def target():
    return SimpleNamespace(id="00000000-0000-0000-0000-000000000001",
                           display_name="Test Alice")


@pytest.fixture
def prepared():
    return SimpleNamespace(checksum="photo-sha", embedding_normalized=[1, 0],
                           width=100, height=100, extension=".jpg")


@pytest.mark.parametrize("scores", [[], [("other", .99)], [("target", .5)],
                                    [("other", .99), ("target", .95)],
                                    [("target", .95), ("other", .95)]])
def test_target_requires_review_when_evidence_is_missing_uncertain_or_conflicting(
        monkeypatch, target, prepared, scores):
    monkeypatch.setattr(service.settings, "ENROLL_STRONG_MATCH_MIN", .8)
    ranked = [(str(target.id) if i == "target" else i, s) for i, s in scores]
    monkeypatch.setattr(service, "find_similar_identities", AsyncMock(return_value=ranked))
    with pytest.raises(service.EnrollmentError) as error:
        run(service.require_target_match(None, target, prepared, actor_user_id=7))
    assert error.value.code == "identity_review_required"
    assert error.value.extra["target_identity_id"] == str(target.id)


def test_confident_target_saves_without_review(monkeypatch, target, prepared):
    monkeypatch.setattr(service.settings, "ENROLL_STRONG_MATCH_MIN", .8)
    monkeypatch.setattr(service, "find_similar_identities",
                        AsyncMock(return_value=[(str(target.id), .95)]))
    run(service.require_target_match(None, target, prepared, actor_user_id=7))


@pytest.mark.parametrize("field,value", [("identity_id", "other"),
                                          ("checksum", "other-photo"),
                                          ("actor_user_id", 8), (None, None)])
def test_review_approval_is_bound_to_target_photo_and_actor(
        monkeypatch, target, prepared, field, value):
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "find_similar_identities", search)
    values = dict(identity_id=str(target.id), checksum=prepared.checksum, actor_user_id=7)
    if field:
        values[field] = value
    call = service.require_target_match(None, target, prepared, actor_user_id=7,
                                        reviewed=service.ReviewedEnrollment(**values))
    if field:
        with pytest.raises(service.EnrollmentError):
            run(call)
    else:
        run(call)
        search.assert_not_awaited()


@pytest.mark.parametrize("by_name", [False, True])
def test_shared_service_refuses_both_paths_before_persistence(
        monkeypatch, target, prepared, by_name):
    db = SimpleNamespace(execute=AsyncMock(return_value=Mock(
        scalar_one_or_none=Mock(return_value=None))), add=Mock(), rollback=AsyncMock())
    writer = SimpleNamespace(save_embedding=AsyncMock())
    monkeypatch.setattr(service, "_identity_service", lambda: writer)
    monkeypatch.setattr(service, "_write_temp_file", Mock(return_value="temp-photo"))
    cleanup = Mock()
    monkeypatch.setattr(service, "_safe_unlink", cleanup)
    monkeypatch.setattr(service, "get_active_identity", AsyncMock(return_value=target))
    monkeypatch.setattr(service, "resolve_identity_by_name", AsyncMock(return_value=target))
    monkeypatch.setattr(service, "find_similar_identities", AsyncMock(return_value=[]))
    kwargs = {"person_name": target.display_name} if by_name else {"identity_id": str(target.id)}
    with pytest.raises(service.EnrollmentError) as error:
        run(service.enroll_image(db, image_bytes=b"photo", original_filename="photo.jpg",
                                 content_type="image/jpeg", prepared=prepared,
                                 actor_user_id=7, **kwargs))
    assert error.value.code == "identity_review_required"
    db.add.assert_not_called()
    writer.save_embedding.assert_not_awaited()
    db.rollback.assert_awaited_once()
    cleanup.assert_called_with("temp-photo")


@pytest.mark.parametrize("by_name", [False, True])
def test_both_routes_return_review_instead_of_success(monkeypatch, target, prepared, by_name):
    refusal = service.EnrollmentError("identity_review_required", "Review needed",
        extra={"target_identity_id": str(target.id), "display_name": target.display_name})
    monkeypatch.setattr(upload, "_read_upload", AsyncMock(return_value=b"photo"))
    monkeypatch.setattr(upload, "prepare_upload", Mock(return_value=prepared))
    monkeypatch.setattr(upload, "resolve_identity_by_name", AsyncMock(return_value=target))
    monkeypatch.setattr(upload, "enroll_image", AsyncMock(side_effect=refusal))
    monkeypatch.setattr(upload, "sweep_expired_pending", AsyncMock())
    payload = {"success": False, "decision_required": True, "upload_token": "ticket"}
    decision = AsyncMock(return_value=payload)
    monkeypatch.setattr(upload, "_decision_payload", decision)
    kwargs = dict(request=None, photo=SimpleNamespace(filename="photo.jpg", content_type="image/jpeg"),
                  is_face_image=False, current_user=SimpleNamespace(id=7, username="admin"),
                  _csrf=None, db=object())
    result = run(upload.upload_person(person_name=target.display_name, **kwargs) if by_name
                 else upload.add_identity_image(identity_id=str(target.id), **kwargs))
    assert result.status_code == 202
    assert decision.await_args.kwargs["target_identity_id"] == str(target.id)


def test_review_keeps_unmatched_target_selectable(monkeypatch, target, prepared):
    monkeypatch.setattr(upload, "find_checksum_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(upload, "find_similar_identities", AsyncMock(return_value=[]))
    build = AsyncMock(return_value=[{"identity_id": str(target.id), "display_name": target.display_name}])
    monkeypatch.setattr(upload, "build_candidate_rows", build)
    park = AsyncMock(return_value=("ticket", SimpleNamespace(expires_at=datetime(2030, 1, 1))))
    monkeypatch.setattr(upload, "create_pending_enrollment", park)
    payload = run(upload._decision_payload(None, prepared=prepared, contents=b"photo",
        display_name=target.display_name, is_face_image=False,
        photo=SimpleNamespace(filename="photo.jpg", content_type="image/jpeg"),
        actor_id=7, target_identity_id=str(target.id)))
    assert payload["decision_required"] is True
    assert payload["recommended_action"] == "review"
    assert payload["match_confidence"] == "uncertain"
    assert build.await_args.args[1] == [(str(target.id), 0.0)]
    park.assert_awaited_once()


@pytest.mark.parametrize("action", ["add_to_existing", "create_new"])
def test_confirmation_binds_approval_and_rejects_existing_new_name(
        monkeypatch, target, prepared, action):
    row = SimpleNamespace(storage_path="pending-photo", file_checksum=prepared.checksum,
                          original_filename="photo.jpg", content_type="image/jpeg",
                          is_face_image=False, display_name=target.display_name,
                          decision="uncertain")
    monkeypatch.setattr(review, "sweep_expired_pending", AsyncMock())
    monkeypatch.setattr(review, "peek_pending_enrollment", AsyncMock(return_value=row))
    monkeypatch.setattr(review, "_verify_models_unchanged", Mock())
    monkeypatch.setattr(review, "_read_pending_bytes", Mock(return_value=b"photo"))
    monkeypatch.setattr(review, "_verify_choice_is_live", AsyncMock(return_value=target))
    monkeypatch.setattr(review, "resolve_identity_by_name", AsyncMock(return_value=target))
    monkeypatch.setattr(review, "prepare_upload", Mock(return_value=prepared))
    claim = AsyncMock(return_value=row)
    monkeypatch.setattr(review, "claim_pending_enrollment", claim)
    cleanup = Mock()
    monkeypatch.setattr(review, "_safe_unlink_pending", cleanup)
    enroll = AsyncMock(return_value=SimpleNamespace(identity_created=False,
        duplicate=False, display_name=target.display_name, identity_id=str(target.id), filename="photo.jpg"))
    monkeypatch.setattr(review, "enroll_image", enroll)
    monkeypatch.setattr(review, "_success_payload", Mock(return_value={"success": True}))
    monkeypatch.setattr(review, "_totals", AsyncMock(return_value=(1, 2)))
    response = run(review.confirm_enrollment(None,
        review.ConfirmRequest(action=action, upload_token="ticket", identity_id=str(target.id)),
        current_user=SimpleNamespace(id=7, username="admin"), _csrf=None, db=object()))
    if action == "create_new":
        assert response.status_code == 409
        assert b"name_already_exists" in response.body
        claim.assert_not_awaited()
        enroll.assert_not_awaited()
        cleanup.assert_not_called()
    else:
        assert response.status_code == 200
        claim.assert_awaited_once()
        assert enroll.await_args.kwargs["reviewed"] == service.ReviewedEnrollment(
            identity_id=str(target.id), checksum=prepared.checksum, actor_user_id=7)


def test_create_new_name_race_cannot_attach_to_existing(monkeypatch, target, prepared):
    db = SimpleNamespace(execute=AsyncMock(), add=Mock(), rollback=AsyncMock())
    monkeypatch.setattr(service, "_identity_service", Mock(return_value=object()))
    monkeypatch.setattr(service, "_write_temp_file", Mock(return_value="temp-photo"))
    monkeypatch.setattr(service, "_safe_unlink", Mock())
    monkeypatch.setattr(service, "resolve_identity_by_name", AsyncMock(return_value=target))
    with pytest.raises(service.EnrollmentError) as error:
        run(service.enroll_image(db, image_bytes=b"photo", original_filename="photo.jpg",
            content_type="image/jpeg", prepared=prepared, actor_user_id=7,
            person_name=target.display_name, require_new=True))
    assert error.value.code == "name_already_exists"
    db.add.assert_not_called()
    db.rollback.assert_awaited_once()


def test_exact_duplicate_returns_without_face_review(monkeypatch, target, prepared):
    stored = SimpleNamespace(id=1, is_primary=True, storage_path="photo.jpg", source_type="upload")
    db = SimpleNamespace(execute=AsyncMock(return_value=Mock(
        scalar_one_or_none=Mock(return_value=stored))))
    monkeypatch.setattr(service, "_identity_service", Mock(return_value=object()))
    monkeypatch.setattr(service, "_write_temp_file", Mock(return_value="temp-photo"))
    monkeypatch.setattr(service, "_safe_unlink", Mock())
    monkeypatch.setattr(service, "get_active_identity", AsyncMock(return_value=target))
    search = AsyncMock()
    monkeypatch.setattr(service, "find_similar_identities", search)
    result = run(service.enroll_image(db, image_bytes=b"photo", original_filename="photo.jpg",
        content_type="image/jpeg", prepared=prepared, actor_user_id=7, identity_id=str(target.id)))
    assert result.duplicate and not result.image_created
    search.assert_not_awaited()
