"""Code identity for Git checkouts and production images, without credentials.

The build records an attestation from the release checkout, then fingerprints
its actual Docker context and installed packages. Runtime accepts that evidence
only while the source and dependency fingerprints still match. This is release
provenance, not a signature or a replacement for trusted image distribution.

Run this file directly at build time: importing backend starts application code.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess

BUILD_MANIFEST_PATH = Path('/usr/local/share/vas/build-provenance.json')
MANIFEST_VERSION = 1
_SOURCE_TREES = ('backend', 'frontend', 'utils', 'sql_agent', 'alembic', 'scripts')
_SOURCE_SUFFIXES = {'.py', '.js', '.css', '.html', '.sh'}
_SKIP_PARTS = {'__pycache__', 'node_modules', '.git', '.venv', 'venv', '.cache'}
_REVISION = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


def application_root():
    return Path(__file__).resolve().parents[2]


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def dependency_versions():
    """Allowlist package names/versions only; never export environment or URLs."""
    versions = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get('Name')
        if name:
            # Keep duplicate distributions visible instead of silently overwriting.
            key = re.sub(r'[-_.]+', '-', name).lower()
            versions.setdefault(key, set()).add(dist.version)
    return {name: sorted(values) for name, values in sorted(versions.items())}


def dependency_fingerprint(dependencies):
    return hashlib.sha256(_json_bytes(dependencies)).hexdigest()


def source_fingerprint(root):
    """Hash executable application inputs, excluding data, secrets and history."""
    root = Path(root)
    paths = set(root.glob('*.py')) | set(root.glob('*.sh')) | set(root.glob('requirements*.txt'))
    paths.update(root.glob('docker/Dockerfile.*'))
    for tree in _SOURCE_TREES:
        paths.update(path for path in (root / tree).rglob('*')
                     if path.suffix in _SOURCE_SUFFIXES
                     and not (_SKIP_PARTS & set(path.relative_to(root).parts)))
    digest = hashlib.sha256()
    count = 0
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError('Application provenance cannot include source symlinks')
        if not path.is_file():
            continue
        content = path.read_bytes()
        relative = path.relative_to(root).as_posix().encode()
        # Lengths make file boundaries unambiguous.
        digest.update(len(relative).to_bytes(8, 'big'))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, 'big'))
        digest.update(content)
        count += 1
    if not count:
        raise ValueError('No application source was found for provenance')
    return digest.hexdigest()


def git_identity(root):
    def git(*args):
        try:
            return subprocess.check_output(
                ['git', '-C', str(root), *args], stderr=subprocess.DEVNULL,
                timeout=10).decode().strip()
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return None
    revision = git('rev-parse', 'HEAD')
    status = git('status', '--porcelain', '--untracked-files=normal')
    return {'git_commit': revision if revision and _REVISION.fullmatch(revision) else None,
            'git_dirty': None if status is None else bool(status)}


def build_arguments(root):
    """Arguments are derived from the checkout, never copied from environment."""
    before = git_identity(root)
    fingerprint = source_fingerprint(root)
    after = git_identity(root)
    if before != after:
        raise ValueError('Checkout changed while recording build provenance; retry the build')
    return {
        'VAS_BUILD_GIT_COMMIT': before['git_commit'] or 'unknown',
        'VAS_BUILD_GIT_DIRTY': ('unknown' if before['git_dirty'] is None
                               else str(before['git_dirty']).lower()),
        'VAS_BUILD_SOURCE_SHA256': fingerprint,
    }


def create_manifest(root, revision='unknown', dirty='unknown', expected_source=''):
    """Seal actual image contents; supplied source hash must match the context."""
    fingerprint = source_fingerprint(root)
    if expected_source and (not _SHA256.fullmatch(expected_source) or expected_source != fingerprint):
        raise ValueError('Docker source differs from the recorded checkout; rerun the provenance build')
    if revision != 'unknown' and not _REVISION.fullmatch(revision):
        raise ValueError('Build Git revision must be a complete commit hash or unknown')
    if dirty not in ('true', 'false', 'unknown'):
        raise ValueError('Build Git dirty state must be true, false or unknown')
    dependencies = dependency_versions()
    requirements = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(Path(root).glob('requirements*.txt'))}
    return {
        'manifest_version': MANIFEST_VERSION,
        'git_commit': None if revision == 'unknown' else revision,
        'git_dirty': None if dirty == 'unknown' else dirty == 'true',
        'source_sha256': fingerprint,
        # Without an expected host hash, a clean revision string is only a label.
        'checkout_source_verified': bool(expected_source),
        'requirements_sha256': requirements,
        'dependencies': dependencies,
        'dependencies_sha256': dependency_fingerprint(dependencies),
    }


def inspect_provenance(root=None, manifest_path=BUILD_MANIFEST_PATH):
    """Return explicit readiness and evidence; unavailable never means clean."""
    root = Path(root) if root is not None else application_root()
    identity = git_identity(root)
    result = {'verified': False, 'source': 'unavailable', 'status': 'unverified',
              'reason': 'Build identity is unavailable. Rebuild with recorded release provenance.',
              **identity}
    try:
        fingerprint = source_fingerprint(root)
    except (OSError, ValueError):
        result.update(status='source_unreadable', reason='Application source could not be verified.')
        return result
    result['source_sha256'] = fingerprint
    # A mounted dirty/unknown checkout must never be hidden by an image manifest.
    if identity['git_commit'] is not None or identity['git_dirty'] is not None:
        clean = identity['git_commit'] is not None and identity['git_dirty'] is False
        result.update(source='git', verified=clean, status='verified' if clean else 'dirty_checkout',
                      reason=None if clean else 'Commit or remove local changes before a reproducible release build.')
        return result
    try:
        path = Path(manifest_path)
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            raise ValueError('Invalid build manifest')
        raw = path.read_bytes()
        manifest = json.loads(raw)
        if not isinstance(manifest, dict) or manifest.get('manifest_version') != MANIFEST_VERSION:
            raise ValueError('Unsupported build manifest')
        revision = manifest.get('git_commit')
        dirty = manifest.get('git_dirty')
        result.update(source='image_build', git_commit=revision, git_dirty=dirty,
                      build_id=hashlib.sha256(raw).hexdigest())
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision) or type(dirty) is not bool:
            result.update(status='unknown_build', reason='Image build did not record a complete Git revision and tree state.')
        elif dirty:
            result.update(status='dirty_build', reason='This image was built from local changes. Build a committed release for reproducible training.')
        elif manifest.get('checkout_source_verified') is not True:
            result.update(status='unverified_build', reason='Image source was not checked against the release checkout. Rebuild with provenance.')
        elif manifest.get('source_sha256') != fingerprint:
            result.update(status='source_mismatch', reason='Application code differs from its image build. Rebuild before reproducible training.')
        elif (not isinstance(manifest.get('dependencies'), dict)
              or manifest.get('dependencies_sha256') != dependency_fingerprint(manifest['dependencies'])
              or manifest['dependencies'] != dependency_versions()):
            result.update(status='dependencies_mismatch', reason='Installed dependencies differ from the image build. Rebuild before reproducible training.')
        else:
            result.update(verified=True, status='verified', reason=None,
                          dependencies_sha256=manifest['dependencies_sha256'])
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        result.update(status='invalid_manifest', reason='The image build manifest is unreadable or invalid. Rebuild the application image.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['args', 'seal', 'check'])
    parser.add_argument('--root', type=Path, default=application_root())
    parser.add_argument('--output', type=Path, default=BUILD_MANIFEST_PATH)
    args = parser.parse_args()
    if args.command == 'args':
        # Each pair stays one shell-array argument; no eval/source is needed.
        for key, value in build_arguments(args.root).items():
            print('--build-arg')
            print(f'{key}={value}')
    elif args.command == 'seal':
        manifest = create_manifest(args.root,
                                   os.environ.get('VAS_BUILD_GIT_COMMIT', 'unknown'),
                                   os.environ.get('VAS_BUILD_GIT_DIRTY', 'unknown'),
                                   os.environ.get('VAS_BUILD_SOURCE_SHA256', ''))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(_json_bytes(manifest))
        args.output.chmod(0o444)
    else:
        result = inspect_provenance(args.root, args.output)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['verified'] else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
