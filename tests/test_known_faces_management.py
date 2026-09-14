"""Known Faces API contracts, with isolated persistence and real route/auth validation."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from backend.auth.auth_service import get_current_user
from backend.routes import known_faces
from db_connection import get_db
from db_models import IdentityStatus, IdentityType

ID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def subject():
    return SimpleNamespace(id=ID, type=IdentityType.KNOWN, display_name="Alice",
        status=IdentityStatus.ACTIVE, merged_into_id=None, best_snapshot_path=None,
        created_at=datetime(2026, 1, 1), last_seen_at=datetime(2026, 1, 2), appearances_count=0)


@pytest.fixture
def context():
    app = FastAPI()
    app.include_router(known_faces.router)
    db = SimpleNamespace(execute=AsyncMock(), add=Mock(), commit=AsyncMock())
    user = SimpleNamespace(id=7, username="admin", role="admin")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, db, user


def test_directory_batches_photos_and_preserves_pagination(context, subject):
    client, db, _ = context
    db.execute.side_effect = [Mock(scalar=Mock(return_value=26)),
        Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[subject])))),
        Mock(all=Mock(return_value=[(ID, 3)])),
        Mock(all=Mock(return_value=[(ID, f"storage/faces/{ID}/image_001.jpg")]))]
    response = client.get('/api/admin/known-faces?page=2&page_size=24&q=Alice%25&sort=name')
    assert response.status_code == 200
    data = response.json()
    assert (data['total'], data['page'], data['total_pages']) == (26, 2, 2)
    assert data['items'][0]['photo_count'] == 3
    assert data['items'][0]['photo_url'] == f'/storage/faces/{ID}/image_001.jpg'
    assert data['items'][0]['last_seen_at'] is None, 'enrollment time is not a camera sighting'
    assert db.execute.await_count == 4
    compiled = db.execute.await_args_list[1].args[0].compile(dialect=postgresql.dialect())
    assert IdentityType.KNOWN in compiled.params.values()
    assert any(isinstance(v, list) and IdentityStatus.PROMOTED in v for v in compiled.params.values())
    assert any(isinstance(v, str) and '\\%' in v for v in compiled.params.values()), 'search escapes wildcards'


@pytest.mark.parametrize('query', ['page=0', 'page_size=101', 'sort=invalid', 'status=invalid'])
def test_invalid_directory_options_are_rejected(context, query):
    client, db, _ = context
    assert client.get('/api/admin/known-faces?' + query).status_code == 422
    db.execute.assert_not_awaited()


def test_empty_directory_skips_photo_queries(context):
    client, db, _ = context
    db.execute.side_effect = [Mock(scalar=Mock(return_value=0)),
        Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[]))))]
    result = client.get('/api/admin/known-faces').json()
    assert result['items'] == [] and result['total'] == 0
    assert db.execute.await_count == 2


def test_name_edit_is_audited_and_does_not_change_last_seen(context, subject):
    client, db, _ = context
    before = subject.last_seen_at
    db.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=subject))
    response = client.patch(f'/api/admin/known-faces/{ID}', json={'display_name': 'Alice Smith'},
                            headers={'X-Requested-With': 'XMLHttpRequest'})
    assert response.status_code == 200
    assert subject.display_name == 'Alice Smith' and subject.last_seen_at == before
    audit = db.add.call_args.args[0]
    assert audit.action_type == 'rename' and audit.username == 'admin'
    assert audit.before_state == {'display_name': 'Alice'}
    assert audit.after_state == {'display_name': 'Alice Smith'}
    db.commit.assert_awaited_once()


@pytest.mark.parametrize('status_code,found', [(404, False), (409, True)])
def test_missing_or_merged_person_cannot_be_renamed(context, subject, status_code, found):
    client, db, _ = context
    subject.status = IdentityStatus.MERGED
    db.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=subject if found else None))
    response = client.patch(f'/api/admin/known-faces/{ID}', json={'display_name': 'New name'},
                            headers={'X-Requested-With': 'XMLHttpRequest'})
    assert response.status_code == status_code
    db.commit.assert_not_awaited()


def test_name_edit_requires_csrf(context):
    client, db, _ = context
    assert client.patch(f'/api/admin/known-faces/{ID}', json={'display_name': 'Alice'}).status_code == 403
    db.execute.assert_not_awaited()


def test_directory_and_name_edit_reject_non_admin(context):
    client, db, user = context
    user.role = 'user'
    assert client.get('/api/admin/known-faces').status_code == 403
    assert client.patch(f'/api/admin/known-faces/{ID}', json={'display_name': 'Alice'},
                        headers={'X-Requested-With': 'XMLHttpRequest'}).status_code == 403
    db.execute.assert_not_awaited()


@pytest.mark.parametrize('method,suffix,body', [
    ('GET', '/deletion-preview', None),
    ('POST', '/activation', {'active': False}),
    ('DELETE', '', {'confirmation_name': 'Alice', 'preview_token': 'a'*64}),
])
def test_lifecycle_endpoints_are_admin_only(context, method, suffix, body):
    client, db, user = context
    user.role = 'user'
    response = client.request(method, f'/api/admin/known-faces/{ID}{suffix}', json=body,
                              headers={'X-Requested-With': 'XMLHttpRequest'})
    assert response.status_code == 403
    db.execute.assert_not_awaited()


@pytest.mark.parametrize('method,suffix,body', [
    ('POST', '/activation', {'active': False}),
    ('DELETE', '', {'confirmation_name': 'Alice', 'preview_token': 'a'*64}),
])
def test_lifecycle_mutations_require_csrf(context, method, suffix, body):
    client, db, _ = context
    response = client.request(method, f'/api/admin/known-faces/{ID}{suffix}', json=body)
    assert response.status_code == 403
    db.execute.assert_not_awaited()
