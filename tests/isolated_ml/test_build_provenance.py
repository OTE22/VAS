"""Run with python tests/isolated_ml/test_build_provenance.py; no app/DB imports."""
import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('vas_build_provenance', REPO / 'backend/ml/build_provenance.py')
provenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provenance)
REVISION = 'a' * 40
DEPENDENCIES = {'numpy': ['1.26.4'], 'scikit-learn': ['1.5.2']}
REAL_GIT_IDENTITY = provenance.git_identity
NO_GIT = {'git_commit': None, 'git_dirty': None}


class BuildProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'backend/ml').mkdir(parents=True)
        (self.root / 'backend/ml/trainer.py').write_text('def train(): return 1\n')
        (self.root / 'config.py').write_text('SETTING = 1\n')
        (self.root / 'requirements-ml.txt').write_text('numpy==1.26.4\n')
        self.manifest = self.root / 'build-manifest.json'
        self.dep_patch = patch.object(provenance, 'dependency_versions', return_value=DEPENDENCIES.copy())
        self.dep_patch.start()
        self.addCleanup(self.dep_patch.stop)
        self.git_patch = patch.object(provenance, 'git_identity', return_value=NO_GIT.copy())
        self.git_patch.start()
        self.addCleanup(self.git_patch.stop)

    def seal(self, **overrides):
        options = {'revision': REVISION, 'dirty': 'false',
                   'expected_source': provenance.source_fingerprint(self.root)}
        options.update(overrides)
        manifest = provenance.create_manifest(self.root, **options)
        self.manifest.write_text(json.dumps(manifest, sort_keys=True))
        return manifest

    def inspect(self):
        return provenance.inspect_provenance(self.root, self.manifest)

    @unittest.skipUnless(shutil.which('git'), 'Git executable is needed for checkout fixture')
    def test_actual_git_checkout_records_clean_and_untracked_changes(self):
        def git(*args):
            subprocess.run(['git', '-C', str(self.root), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git('init')
        git('add', '.')
        git('-c', 'user.name=Provenance test', '-c', 'user.email=tests@example.invalid',
            'commit', '--no-gpg-sign', '-m', 'isolated test fixture')
        identity = REAL_GIT_IDENTITY(self.root)
        self.assertIs(identity['git_dirty'], False)
        self.assertEqual(len(identity['git_commit']), 40)
        (self.root / 'backend/ml/local.py').write_text('LOCAL = True\n')
        self.assertIs(REAL_GIT_IDENTITY(self.root)['git_dirty'], True)
        (self.root / 'backend/ml/local.py').unlink()
        (self.root / 'config.py').write_text('SETTING = 2\n')
        self.assertIs(REAL_GIT_IDENTITY(self.root)['git_dirty'], True)

    def test_image_can_verify_without_git(self):
        self.seal()
        result = self.inspect()
        self.assertTrue(result['verified'])
        self.assertEqual(result['source'], 'image_build')
        self.assertEqual(result['git_commit'], REVISION)
        self.assertFalse(result['git_dirty'])
        self.assertEqual(len(result['build_id']), 64)

    def test_no_manifest_is_unknown_not_clean(self):
        result = self.inspect()
        self.assertFalse(result['verified'])
        self.assertEqual(result['status'], 'unverified')
        self.assertIsNone(result['git_dirty'])

    def test_direct_build_cannot_claim_clean_without_host_hash(self):
        self.seal(expected_source='')
        self.assertEqual(self.inspect()['status'], 'unverified_build')

    def test_dirty_build_is_rejected(self):
        self.seal(dirty='true')
        self.assertEqual(self.inspect()['status'], 'dirty_build')
        self.assertFalse(self.inspect()['verified'])

    def test_unknown_build_is_rejected(self):
        self.seal(revision='unknown', dirty='unknown')
        self.assertEqual(self.inspect()['status'], 'unknown_build')

    def test_build_rejects_source_change_between_host_and_docker(self):
        original = provenance.source_fingerprint(self.root)
        (self.root / 'backend/ml/trainer.py').write_text('def train(): return 2\n')
        with self.assertRaisesRegex(ValueError, 'Docker source differs'):
            self.seal(expected_source=original)

    def test_runtime_code_change_is_rejected(self):
        self.seal()
        (self.root / 'backend/ml/trainer.py').write_text('def train(): return 2\n')
        self.assertEqual(self.inspect()['status'], 'source_mismatch')

    def test_runtime_config_change_is_rejected(self):
        self.seal()
        (self.root / 'config.py').write_text('SETTING = 2\n')
        self.assertEqual(self.inspect()['status'], 'source_mismatch')

    def test_new_source_file_is_detected(self):
        self.seal()
        (self.root / 'backend/ml/injected.py').write_text('VALUE = 1\n')
        self.assertEqual(self.inspect()['status'], 'source_mismatch')

    def test_dependency_change_is_rejected(self):
        self.seal()
        with patch.object(provenance, 'dependency_versions', return_value={'numpy': ['2.0']}):
            self.assertEqual(self.inspect()['status'], 'dependencies_mismatch')

    def test_dependency_evidence_tampering_is_rejected(self):
        manifest = self.seal()
        manifest['dependencies']['numpy'] = ['0.1']
        self.manifest.write_text(json.dumps(manifest))
        self.assertEqual(self.inspect()['status'], 'dependencies_mismatch')

    def test_requirements_changes_are_detected(self):
        self.seal()
        (self.root / 'requirements-ml.txt').write_text('numpy==2.0\n')
        self.assertEqual(self.inspect()['status'], 'source_mismatch')

    def test_secrets_data_and_environment_are_not_exported(self):
        baseline = provenance.source_fingerprint(self.root)
        (self.root / '.env').write_text('PASSWORD=must-not-export\n')
        (self.root / 'secrets').mkdir()
        (self.root / 'secrets/password').write_text('must-not-export')
        (self.root / 'storage').mkdir()
        (self.root / 'storage/camera.jpg').write_bytes(b'private')
        with patch.dict('os.environ', {'PROVENANCE_TEST_SECRET': 'must-not-export'}):
            manifest = self.seal()
        self.assertEqual(baseline, provenance.source_fingerprint(self.root))
        self.assertNotIn('must-not-export', json.dumps(manifest))
        self.assertEqual(manifest['requirements_sha256'].keys(), {'requirements-ml.txt'})

    def test_git_dirty_cannot_fall_back_to_clean_image(self):
        self.seal()
        with patch.object(provenance, 'git_identity', return_value={'git_commit': REVISION, 'git_dirty': True}):
            result = self.inspect()
        self.assertEqual(result['source'], 'git')
        self.assertEqual(result['status'], 'dirty_checkout')
        self.assertFalse(result['verified'])

    def test_clean_git_checkout_remains_supported(self):
        with patch.object(provenance, 'git_identity', return_value={'git_commit': REVISION, 'git_dirty': False}):
            result = self.inspect()
        self.assertEqual(result['source'], 'git')
        self.assertTrue(result['verified'])

    def test_malformed_manifest_is_rejected(self):
        for value in ('not json', '[]', '{"manifest_version": 999}'):
            with self.subTest(value=value):
                self.manifest.write_text(value)
                self.assertEqual(self.inspect()['status'], 'invalid_manifest')

    def test_untyped_clean_flag_is_rejected(self):
        manifest = self.seal()
        manifest['git_dirty'] = 'false'
        self.manifest.write_text(json.dumps(manifest))
        self.assertEqual(self.inspect()['status'], 'unknown_build')

    def test_manifest_symlink_is_rejected(self):
        self.seal()
        target = self.root / 'other.json'
        self.manifest.rename(target)
        self.manifest.symlink_to(target)
        self.assertEqual(self.inspect()['status'], 'invalid_manifest')

    def test_source_symlink_is_rejected(self):
        (self.root / 'backend/ml/linked.py').symlink_to(self.root / 'config.py')
        self.assertEqual(self.inspect()['status'], 'source_unreadable')

    def test_arguments_are_derived_not_trusted_from_environment(self):
        with patch.dict('os.environ', {'VAS_BUILD_GIT_COMMIT': REVISION, 'VAS_BUILD_GIT_DIRTY': 'false'}):
            args = provenance.build_arguments(self.root)
        self.assertEqual(args['VAS_BUILD_GIT_COMMIT'], 'unknown')
        self.assertEqual(args['VAS_BUILD_GIT_DIRTY'], 'unknown')
        self.assertEqual(len(args['VAS_BUILD_SOURCE_SHA256']), 64)

    def test_git_changing_during_capture_is_rejected(self):
        with patch.object(provenance, 'git_identity', side_effect=[
                {'git_commit': REVISION, 'git_dirty': False},
                {'git_commit': REVISION, 'git_dirty': True}]):
            with self.assertRaisesRegex(ValueError, 'Checkout changed'):
                provenance.build_arguments(self.root)

    def test_capture_accepts_verified_build_and_preserves_evidence(self):
        capture_spec = importlib.util.spec_from_file_location('vas_reproducibility', REPO / 'backend/ml/reproducibility.py')
        capture_module = importlib.util.module_from_spec(capture_spec)
        capture_spec.loader.exec_module(capture_module)
        capture_module.__file__ = str(self.root / 'backend/ml/reproducibility.py')
        registry = types.ModuleType('backend.ml.registry_service')
        class RegistryError(Exception):
            def __init__(self, code, message):
                self.code = code
                super().__init__(message)
        registry.RegistryError = RegistryError
        modules = {'backend': types.ModuleType('backend'), 'backend.ml': types.ModuleType('backend.ml'),
                   'backend.ml.registry_service': registry, 'backend.ml.build_provenance': provenance}
        identity = {'verified': True, 'source': 'image_build', 'git_commit': REVISION,
                    'git_dirty': False, 'status': 'verified', 'reason': None}
        with patch.dict(sys.modules, modules), patch.object(provenance, 'inspect_provenance', return_value=identity):
            manifest = capture_module.capture(42, {}, require_clean=True)
        self.assertEqual(manifest['code_identity'], identity)
        self.assertEqual(manifest['git_commit'], REVISION)
        self.assertFalse(manifest['git_dirty'])
        with patch.dict(sys.modules, modules), patch.object(provenance, 'inspect_provenance', return_value={
                **identity, 'verified': False, 'reason': 'Image code has changed.'}):
            with self.assertRaisesRegex(RegistryError, 'Image code has changed') as caught:
                capture_module.capture(42, {}, require_clean=True)
            self.assertEqual(caught.exception.code, 'REPRODUCIBILITY_CODE_UNVERIFIED')


if __name__ == '__main__':
    unittest.main()
