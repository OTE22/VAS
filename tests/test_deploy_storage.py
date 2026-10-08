"""Storage safeguards without Docker mutations or application database access."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vas_storage', REPO / 'scripts/deploy/storage_layout.py')
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vas-storage-')
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = str(self.parent / 'data disk')

    def volume(self, name='postgres_data', root=None):
        return {name: {'Name': storage.PREFIX + name, 'Driver': 'local',
                       'Mountpoint': '/custom-docker-data/volumes/' + name,
                       'Options': {'type': 'none', 'o': 'bind', 'device': f'{root}/{name}'} if root else {}}}

    def test_default_retains_existing_named_volume(self):
        storage.check_layout('', self.volume())
        storage.prepare('', self.volume())
        self.assertFalse(Path(self.root).exists())

    def test_default_to_custom_rejected_before_writing(self):
        with self.assertRaisesRegex(ValueError, 'offline migration'):
            storage.prepare(self.root, self.volume())
        self.assertFalse(Path(self.root).exists())

    def test_custom_to_default_and_different_directory_rejected(self):
        for selected in ('', self.root + '-different'):
            with self.subTest(selected=selected), self.assertRaisesRegex(ValueError, 'existing storage differs'):
                storage.check_layout(selected, self.volume(root=self.root))

    def test_missing_disk_does_not_recreate_existing_data(self):
        with self.assertRaisesRegex(ValueError, 'Mount the original disk'):
            storage.prepare(self.root, self.volume(root=self.root))
        self.assertFalse(Path(self.root).exists())

    def test_matching_existing_data_is_preserved(self):
        target = Path(self.root) / 'postgres_data'
        target.mkdir(parents=True)
        (target / 'database-marker').write_text('existing')
        storage.check_layout(self.root, self.volume(root=self.root))
        self.assertEqual((target / 'database-marker').read_text(), 'existing')

    def test_nonempty_unregistered_data_rejected(self):
        target = Path(self.root) / 'postgres_data'
        target.mkdir(parents=True)
        (target / 'database-marker').write_text('existing')
        with self.assertRaisesRegex(ValueError, 'contains data'):
            storage.prepare(self.root, {})
        self.assertFalse((Path(self.root) / 'redis_data').exists())

    def test_new_directories_receive_service_ownership_without_recursion(self):
        with patch.object(storage.os, 'chown') as chown:
            storage.prepare(self.root, {})
        self.assertEqual(chown.call_count, len(storage.VOLUMES))
        for name, (uid, gid, _) in storage.VOLUMES.items():
            target = Path(self.root) / name
            self.assertTrue(target.is_dir())
            chown.assert_any_call(target, uid, gid)

    def test_unsafe_root_and_symlinks_rejected(self):
        for value in ['/', '/srv', '/etc/vas', 'relative/path', '/srv/../data',
                      '/srv/${EVIL}', "/srv/a'quote", '/srv/a\nENV=x', '/srv/vas/', '/srv//vas']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                storage.validate_root(value)
        target = self.parent / 'actual'; target.mkdir()
        link = self.parent / 'link'; link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            storage.validate_root(str(link / 'data'))

    def test_symlink_volume_directory_is_rejected(self):
        Path(self.root).mkdir()
        (Path(self.root) / 'postgres_data').symlink_to(self.parent)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            storage.check_layout(self.root, {})

    def test_missing_parent_disk_must_be_prepared(self):
        with self.assertRaisesRegex(ValueError, 'Mount/prepare'):
            storage.prepare(self.root + '/missing/data', {})
        self.assertFalse(Path(self.root).exists())

    def test_report_uses_actual_docker_data_root_and_lists_assets(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            storage.report('', self.volume(), self.parent)
        text = output.getvalue()
        for expected in ['/custom-docker-data/volumes/postgres_data', 'det_10g.onnx',
                         'w600k_r50.onnx', 'WEIGHTS_MANIFEST.json', '*.mbtiles',
                         'production/fonts', 'ml_artifacts_data', 'backup_data', 'Jupyter']:
            self.assertIn(expected, text)
        self.assertNotIn('/var/lib/docker', text)

    def test_unavailable_docker_does_not_mean_empty_installation(self):
        with patch.object(storage.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'docker')):
            with self.assertRaises(subprocess.CalledProcessError):
                storage.docker_volumes()

    def test_custom_storage_refuses_remote_docker(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'ssh://remote', 'DOCKER_CONTEXT': ''}):
            with self.assertRaisesRegex(ValueError, 'local Linux Docker'):
                storage.require_local_docker()

    def test_docker_context_overrides_host_variable(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': 'remote'}):
            with patch.object(storage.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ssh://remote')):
                with self.assertRaisesRegex(ValueError, 'local Linux Docker'):
                    storage.require_local_docker()

    def test_wrong_existing_owner_is_reported_without_chown(self):
        target = Path(self.root) / 'storage_data'
        target.mkdir(parents=True)
        with patch.dict(storage.VOLUMES, {'storage_data': (999999, 999999, 'photos')}):
            with patch.object(storage.os, 'chown') as chown:
                with self.assertRaisesRegex(ValueError, 'requires owner'):
                    storage.prepare(self.root, self.volume('storage_data', self.root))
                chown.assert_not_called()

    def test_inspection_only_targets_this_projects_declared_volumes(self):
        records = list(self.volume().values())
        def docker(args, **kwargs):
            if args[1:3] == ['volume', 'ls']:
                return subprocess.CompletedProcess(args, 0, storage.PREFIX + 'postgres_data\nother_private_volume\n')
            self.assertEqual(args, ['docker', 'volume', 'inspect', storage.PREFIX + 'postgres_data'])
            return subprocess.CompletedProcess(args, 0, json.dumps(records))
        with patch.object(storage.subprocess, 'run', side_effect=docker):
            self.assertEqual(storage.docker_volumes(), self.volume())


if __name__ == '__main__':
    unittest.main()
