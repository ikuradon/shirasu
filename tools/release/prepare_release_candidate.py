#!/usr/bin/env python3
"""Prepare and verify an isolated image/receiver release candidate.

Only the emitted artifacts register v5. The checkout and the normal image
package retain v1-v4. The manifest identifies the complete snapshot that must
be tested and, if approved for release, promoted without rebuilding it from a
later working tree. Its hash is an integrity identifier, not a signature.
"""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from tools.release import prepare_image_service as images
from tools.release.layout import (LEGACY_IMAGE_FILES, LEGACY_RECEIVER_FILES,
                                  LEGACY_PACKAGED_IMAGE_FILES, LEGACY_PACKAGED_RECEIVER_FILES,
                                  SCHEMA2_IMAGE_FILES, SCHEMA2_RECEIVER_FILES,
                                  MOVED_FILES, PACKAGE_INITS, PROFILE_PATH, profile_path)


CANDIDATE_VERSION = 'v5'
ROLLBACK_VERSION = 'v4'
MANIFEST_NAME = 'candidate-manifest.json'
MANIFEST_SCHEMA = 3
SUPPORTED_MANIFEST_SCHEMAS = (1, 2, MANIFEST_SCHEMA)
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_BUNDLE_BYTES = 512 * 1024 * 1024

# Keep this explicit: config.py, .env files, live state, signing keys and user
# overrides must never be discovered by a glob or copied into either artifact.
RECEIVER_FILES = (
    *PACKAGE_INITS, 'shirasu/__main__.py', 'shirasu/cli.py', 'shirasu/health.py',
    *MOVED_FILES.values(),
    'shirasu/reports/publication.py',
    'requirements/receiver.txt', 'requirements/test.txt', 'Dockerfile', '.dockerignore',
    'LICENSE', 'licenses/README.md', 'licenses/ACKNOWLEDGEMENTS.txt',
    'licenses/pyserial-3.5-LICENSE.txt',
)
IMAGE_GENERATED_FILES = ('requirements.txt', 'api/image.py', '.python-version', 'vercel.json')
CANDIDATE_PROFILE = '''"""Frozen release-candidate profile; generated only in isolated artifacts."""

RENDERERS = {'v1': 'image_service.render', 'v2': 'image_service.render_v2',
             'v3': 'image_service.render_v3', 'v4': 'image_service.render_v4',
             'v5': 'image_service.render_preview'}
SUPPORTED_VERSIONS = tuple(RENDERERS)
NANKAI_TOPIC_VERSIONS = ('v5',)
WEBP_VERSIONS = ('v5',)
CANDIDATE_VERSION = 'v5'
'''


def _names(*, legacy=False, schema_version=MANIFEST_SCHEMA):
    image_names = (*images.FILES, *images.PREVIEW_FILES, *IMAGE_GENERATED_FILES, 'Dockerfile')
    receiver_names = RECEIVER_FILES
    if legacy:
        image_names = LEGACY_IMAGE_FILES
        receiver_names = LEGACY_RECEIVER_FILES
    elif schema_version == 1:
        image_names = LEGACY_PACKAGED_IMAGE_FILES
        receiver_names = LEGACY_PACKAGED_RECEIVER_FILES
    elif schema_version == 2:
        image_names = SCHEMA2_IMAGE_FILES
        receiver_names = SCHEMA2_RECEIVER_FILES
    return {*(f'images/{name}' for name in image_names),
            *(f'receiver/{name}' for name in receiver_names)}


def _safe_name(value):
    if (not isinstance(value, str) or len(value) > 512
            or re.fullmatch(r'[A-Za-z0-9_./-]+', value) is None
            or PurePosixPath(value).is_absolute()
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Candidate manifest contains an unsafe file path')
    return value


def _source_file(root, name):
    path = root
    for part in _safe_name(name).split('/'):
        path /= part
        if path.is_symlink():
            raise ValueError('Linked source is not allowed: ' + name)
    if not path.is_file():
        if (not path.exists() and name.startswith('image_service/assets/')
                and name != 'image_service/assets/v1/manifest.json'):
            raise ValueError('Prepare vendor assets first: python3 -B tools/assets/prepare_vendor_assets.py'
                             f' (missing: {name})')
        raise ValueError('Missing candidate source: ' + name)
    return path


def _digest(path):
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as source:
        while data := source.read(1024 * 1024):
            size += len(data)
            if size > MAX_FILE_BYTES:
                raise ValueError('Candidate file exceeds the size limit')
            digest.update(data)
    return digest.hexdigest(), size


def _source_info():
    try:
        commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        status = subprocess.run(['git', '--no-optional-locks', '-C', str(ROOT), 'status',
                                 '--porcelain', '--untracked-files=normal'],
                                capture_output=True, text=True, check=True, timeout=5).stdout
        if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit) is None:
            raise ValueError('Invalid Git commit')
        return {'commit': commit, 'working_tree': bool(status.strip())}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'commit': None, 'working_tree': None}


def prepare(destination):
    """Create a new allowlisted bundle; never overwrite or read live settings."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Candidate destination already exists: ' + str(destination))
    for name in (*images.FILES, *images.PREVIEW_FILES, 'image_service/requirements.txt',
                 'image_service/Dockerfile', *RECEIVER_FILES):
        _source_file(ROOT, name)
    provenance = _source_info()
    destination.mkdir(parents=True, exist_ok=False)
    images.prepare(destination / 'images', include_preview=True)
    shutil.copyfile(ROOT / 'image_service/Dockerfile', destination / 'images/Dockerfile')
    (destination / 'receiver').mkdir()
    for name in RECEIVER_FILES:
        target = destination / 'receiver' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    for package in ('images', 'receiver'):
        (destination / package / PROFILE_PATH).write_text(CANDIDATE_PROFILE, encoding='utf-8')
    files = {name: _digest(destination / name)[0] for name in sorted(_names())}
    manifest = {'schema_version': MANIFEST_SCHEMA, 'candidate_version': CANDIDATE_VERSION,
                'rollback_version': ROLLBACK_VERSION, 'source': provenance, 'files': files}
    with (destination / MANIFEST_NAME).open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')
    verify_bundle(destination)
    return destination


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Candidate manifest contains duplicate JSON keys')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('Candidate manifest contains a non-finite number')


def verify_bundle(directory):
    """Validate every declared byte and reject extra/missing/linked files.

    Returns the manifest plus ``manifest_sha256``. The latter fingerprints the
    original manifest bytes and is not written back into the candidate. The
    caller should retain that fingerprint alongside its verification results.
    No artifact code is imported or executed by this function.
    """
    root = Path(directory).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Candidate bundle must be an existing unlinked directory')
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('Candidate manifest is missing or linked')
    with manifest_path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('Candidate manifest exceeds the size limit')
    try:
        manifest = json.loads(raw, object_pairs_hook=_object, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Candidate manifest is not valid JSON') from error
    if (not isinstance(manifest, dict)
            or set(manifest) != {'schema_version', 'candidate_version', 'rollback_version', 'source', 'files'}
            or type(manifest['schema_version']) is not int
            or manifest['schema_version'] not in SUPPORTED_MANIFEST_SCHEMAS
            or manifest['candidate_version'] != CANDIDATE_VERSION
            or manifest['rollback_version'] != ROLLBACK_VERSION):
        raise ValueError('Candidate manifest version fields are invalid')
    source = manifest['source']
    if (not isinstance(source, dict) or set(source) != {'commit', 'working_tree'}
            or (source['commit'] is None) != (source['working_tree'] is None)
            or (source['commit'] is not None and
                (not isinstance(source['commit'], str)
                 or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', source['commit']) is None
                 or type(source['working_tree']) is not bool))):
        raise ValueError('Candidate source provenance is invalid')
    declared = manifest['files']
    if not isinstance(declared, dict):
        raise ValueError('Candidate files must be a hash mapping')
    for name, digest in declared.items():
        _safe_name(name)
        if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
            raise ValueError('Candidate file hash is invalid')
    allowed = ((_names(schema_version=1), _names(legacy=True))
               if manifest['schema_version'] == 1
               else (_names(schema_version=manifest['schema_version']),))
    if set(declared) not in allowed:
        raise ValueError('Candidate manifest differs from the distribution allowlist')
    expected = set(declared) | {MANIFEST_NAME}
    directories = {str(parent) for name in declared for parent in PurePosixPath(name).parents
                   if str(parent) != '.'}
    actual, total = set(), 0
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError('Candidate bundle contains a symbolic link: ' + name)
        if path.is_dir():
            if name not in directories:
                raise ValueError('Candidate bundle contains an unexpected directory: ' + name)
            continue
        if not path.is_file() or name not in expected:
            raise ValueError('Candidate bundle contains an unexpected file: ' + name)
        actual.add(name)
        if name == MANIFEST_NAME:
            continue
        digest, size = _digest(path)
        total += size
        if total > MAX_BUNDLE_BYTES:
            raise ValueError('Candidate bundle exceeds the size limit')
        if digest != declared[name]:
            raise ValueError('Candidate file hash differs: ' + name)
    if actual != expected:
        raise ValueError('Candidate bundle is missing declared files')
    for package in ('images', 'receiver'):
        if (root / package / profile_path(manifest)).read_text(encoding='utf-8') != CANDIDATE_PROFILE:
            raise ValueError('Candidate generation profile differs from the verified profile')
    return {**manifest, 'manifest_sha256': hashlib.sha256(raw).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='new candidate bundle directory')
    parser.add_argument('--verify', action='store_true', help='verify an existing bundle without modifying it')
    args = parser.parse_args(argv)
    try:
        if not args.verify:
            prepare(args.destination)
        result = verify_bundle(args.destination)
    except (OSError, ValueError) as error:
        print('Release candidate failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps({'bundle': str(args.destination.absolute()),
                      'candidate_version': result['candidate_version'],
                      'manifest_sha256': result['manifest_sha256'],
                      'files': len(result['files'])}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
