#!/usr/bin/env python3
"""Read-only storage planning/validation, and explicit creation of NEW directories.

Never migrate, remove, or recursively change ownership of existing data.
Uses Docker's actual volume locations, not an assumed /var/lib/docker root.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

VOLUMES = {
    'postgres_data': (0, 0, 'PostgreSQL database'),
    'redis_data': (0, 0, 'Redis persistence'),
    'storage_data': (1000, 1000, 'face photos, snapshots and application artifacts'),
    'logs_data': (1000, 1000, 'application logs'),
    'face_database_data': (1000, 1000, 'local face indexes'),
    'ml_artifacts_data': (1000, 1000, 'trained ML models and manifests'),
    'chromadb_cache': (1000, 1000, 'Chroma embedding model cache'),
    'hf_cache_data': (1000, 1000, 'Hugging Face model cache'),
    'vllm_models': (0, 0, 'vLLM models (optional profile)'),
    'milvus_etcd': (0, 0, 'Milvus metadata (optional profile)'),
    'milvus_minio': (0, 0, 'Milvus objects (optional profile)'),
    'milvus_data': (0, 0, 'Milvus vectors (optional profile)'),
    'ollama_models': (0, 0, 'Ollama language models'),
    'backup_data': (0, 0, 'automatic application backups'),
    'prometheus_data': (65534, 65534, 'Prometheus metrics'),
    'grafana_data': (472, 0, 'Grafana data'),
}
PREFIX = 'face_detector_prod_'


def validate_root(value):
    if not value:
        return
    # Deliberately restrictive: the value goes through dotenv and Compose.
    # Spaces are supported; interpolation, separators and control chars are not.
    if not re.fullmatch(r'/[A-Za-z0-9_./ -]+', value) or value != value.strip():
        raise ValueError('Use an absolute Linux directory with letters, digits, spaces, /, ., _ or -.')
    path = Path(value)
    if str(path) != value or '..' in path.parts or len(path.parts) < 3:
        raise ValueError('Use a dedicated directory such as /srv/vas-data, not / or /srv.')
    if path.parts[1] in ('etc', 'dev', 'proc', 'sys', 'bin', 'sbin', 'lib', 'lib64', 'boot', 'run'):
        raise ValueError('System directories cannot be used for application data.')
    if path.resolve() != path:
        raise ValueError('Use the real storage path; symlinks are not supported.')
    if path.exists() and not path.is_dir():
        raise ValueError(f'Not a directory: {path}')


def require_local_docker():
    # A bind device refers to the DAEMON host; never prepare a local directory
    # while deploying through a remote Docker context.
    endpoint = os.environ.get('DOCKER_HOST', '') if not os.environ.get('DOCKER_CONTEXT') else ''
    if not endpoint:
        result = subprocess.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
                                check=True, text=True, capture_output=True, timeout=30)
        endpoint = result.stdout.strip()
    if not endpoint.startswith('unix://'):
        raise ValueError('Custom host storage requires a local Linux Docker daemon over a Unix socket.')


def docker_volumes():
    listing = subprocess.run(['docker', 'volume', 'ls', '--format', '{{.Name}}'],
                             check=True, text=True, capture_output=True, timeout=30)
    names = sorted(set(listing.stdout.splitlines()) & {PREFIX + key for key in VOLUMES})
    if not names:
        return {}
    result = subprocess.run(['docker', 'volume', 'inspect', *names], check=True,
                            text=True, capture_output=True, timeout=30)
    records = json.loads(result.stdout)
    if {item['Name'] for item in records} != set(names):
        raise ValueError('Docker volume inventory changed; rerun the storage check.')
    return {item['Name'][len(PREFIX):]: item for item in records}


def check_layout(root, volumes):
    validate_root(root)
    problems = []
    for name in VOLUMES:
        volume = volumes.get(name)
        desired = {'type': 'none', 'o': 'bind', 'device': f'{root}/{name}'} if root else {}
        if volume and (volume.get('Driver') != 'local' or (volume.get('Options') or {}) != desired):
            problems.append(f'{PREFIX}{name}: existing storage differs from the requested layout. '
                            'Keep the previous VAS_DATA_ROOT; relocation requires a backed-up, offline migration.')
        if not root:
            continue
        path = Path(root) / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            problems.append(f'{path}: expected a real directory, not a symlink or file.')
        elif volume and not path.is_dir():
            problems.append(f'{path}: existing volume directory missing. Mount the original disk; '
                            'the installer will not create an empty replacement.')
        elif not volume and path.exists() and any(path.iterdir()):
            problems.append(f'{path}: contains data without a matching Docker volume. '
                            'Use an empty directory for a new installation or perform an explicit restore.')
        elif path.is_dir() and VOLUMES[name][0] != 0:
            uid, gid, _ = VOLUMES[name]
            stat = path.stat()
            if stat.st_uid != uid or stat.st_gid != gid or stat.st_mode & 0o700 != 0o700:
                problems.append(f'{path}: requires owner {uid}:{gid} with owner rwx; '
                                'review existing ownership before changing it.')
    if problems:
        raise ValueError('\n'.join(problems))
    if root and not Path(root).parent.is_dir():
        raise ValueError(f'Parent directory {Path(root).parent} missing. Mount/prepare the storage disk first.')


def prepare(root, volumes):
    check_layout(root, volumes)  # Complete all checks before creating anything.
    if not root:
        return
    path = Path(root)
    if not path.exists():
        path.mkdir(mode=0o750)
    for name, (uid, gid, _) in VOLUMES.items():
        target = path / name
        if not target.exists():
            target.mkdir(mode=0o755 if uid == 0 else 0o750)
            os.chown(target, uid, gid)
    print(f'Host directories ready under {root}; no existing files moved or overwritten.')


def report(root, volumes, checkout):
    print('\nPersistent data (container paths stay unchanged):')
    for name, (_, _, purpose) in VOLUMES.items():
        record = volumes.get(name)
        if record:
            location = (record.get('Options') or {}).get('device') or record.get('Mountpoint', 'unknown')
        else:
            location = f'{root}/{name}' if root else f'Docker volume {PREFIX}{name} (created on first start)'
        print(f'  {purpose}\n    {location}')
    print('\nFiles to supply in the release checkout:')
    for relative, purpose in (
        ('weights/det_10g.onnx', 'SCRFD detector; verified release weight'),
        ('weights/w600k_r50.onnx', 'ArcFace recognizer; verified release weight'),
        ('weights/WEIGHTS_MANIFEST.json', 'trusted checksum manifest for BOTH weights'),
        ('map-data/production/*.mbtiles', 'map archives; content verification still required'),
        ('map-data/production/fonts/', 'map font files (.ttf / .otf)'),
        ('map-data/metadata/', 'writable map catalogue/checksums/verdicts'),
        ('backups/', 'host deployment database dumps / upgrade snapshots'),
        ('logs/deploy/', 'installer logs'),
        ('certs/', 'HTTPS certificates and protected private keys'),
        ('secrets/', 'generated credentials; do not share'),
        ('docker/.env', 'deployment configuration; contains credentials'),
        ('.deployment/', 'installer state'),
    ):
        print(f'  {checkout / relative}\n    {purpose}')
    print('ONNX/maps remain in the checkout even when VAS_DATA_ROOT is set.')
    print('Do not copy files directly into database volumes. Use the application/import or restore workflow.')
    print('Container stdout logs and image/build caches remain under Docker daemon storage, outside VAS_DATA_ROOT.')
    print('Notebook storage belongs to its separately deployed Jupyter stack.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check-root', 'check', 'prepare', 'report', 'plan'))
    parser.add_argument('--data-root', default='')
    parser.add_argument('--checkout', type=Path, required=True)
    args = parser.parse_args()
    validate_root(args.data_root)
    if args.action == 'check-root':
        return
    if args.data_root and args.action in ('check', 'prepare'):
        require_local_docker()
    volumes = {} if args.action == 'plan' else docker_volumes()
    if args.action in ('check', 'prepare'):
        check_layout(args.data_root, volumes)
    if args.action == 'prepare':
        prepare(args.data_root, volumes)
    if args.action in ('report', 'plan'):
        report(args.data_root, volumes, args.checkout)
    if args.action == 'report':
        check_layout(args.data_root, volumes)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'Storage check failed: {error}', file=sys.stderr)
        sys.exit(1)
