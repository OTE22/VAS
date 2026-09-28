"""Release-image metadata must reach dataset/model engineering checks."""
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from conftest import settings
from backend.core import runtime_fingerprint
from backend.ml import build_provenance


def test_verified_image_revision_is_used_by_shared_resolver(monkeypatch):
    monkeypatch.setattr(settings,'GIT_COMMIT','',raising=False)
    with patch.object(build_provenance,'inspect_provenance',return_value={'verified':True,'git_commit':'a'*40}):
        assert runtime_fingerprint._git_commit()=='a'*40
        from backend.ml.dataset_builder import _code_version
        assert _code_version()=='a'*40


@pytest.mark.parametrize('identity',[{'verified':False,'git_commit':'a'*40},{'verified':False,'git_commit':None}])
def test_unverified_image_never_claims_manifest_revision(monkeypatch,identity):
    monkeypatch.setattr(settings,'GIT_COMMIT','',raising=False)
    with patch.object(build_provenance,'inspect_provenance',return_value=identity), \
         patch('builtins.open',side_effect=FileNotFoundError), \
         patch.object(runtime_fingerprint.subprocess,'run',return_value=SimpleNamespace(stdout=b'')):
        assert runtime_fingerprint._git_commit()=='unknown'


def test_explicit_runtime_revision_retains_precedence(monkeypatch):
    monkeypatch.setattr(settings,'GIT_COMMIT','explicit-release',raising=False)
    with patch.object(build_provenance,'inspect_provenance') as inspect:
        assert runtime_fingerprint._git_commit()=='explicit-release'
        inspect.assert_not_called()
