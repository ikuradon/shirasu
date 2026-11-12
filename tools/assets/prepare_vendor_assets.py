#!/usr/bin/env python3
"""Materialize checksum-pinned vendor snapshots at their unchanged runtime paths.

No downloads or GIS conversions occur here. Existing equal files are retained;
different files, symlinks and an incomplete lock are rejected without overwrite.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from tools.release.prepare_image_service import FILES, PREVIEW_FILES

MANIFEST = 'vendor/manifest.json'
MAX_MANIFEST_BYTES = 128 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
DESTINATIONS = frozenset(name for name in (*FILES, *PREVIEW_FILES)
                         if name.startswith('image_service/assets/')
                         and name != 'image_service/assets/v1/manifest.json')


def require(condition, message):
    if not condition:
        raise ValueError('Vendor assets: ' + message)


def path_at(root, name):
    require(isinstance(name, str) and len(name) <= 512
            and re.fullmatch(r'[A-Za-z0-9_./-]+', name)
            and not PurePosixPath(name).is_absolute()
            and all(part not in ('', '.', '..') for part in name.split('/')),
            'invalid asset path')
    path = root / name
    require(not any(part.is_symlink() for part in (path, *path.parents)),
            'linked asset paths are not allowed: ' + name)
    return path


def fingerprint(path):
    require(path.is_file(), 'expected a regular file: ' + path.name)
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            require(size <= MAX_FILE_BYTES, 'asset exceeds the size limit')
            digest.update(chunk)
    return digest.hexdigest(), size


def load_lock(root):
    path = path_at(root, MANIFEST)
    require(path.is_file(), 'missing vendor manifest')
    with path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    require(len(raw) <= MAX_MANIFEST_BYTES, 'manifest exceeds the size limit')

    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'duplicate manifest key')
            value[key] = item
        return value

    def constant(_):
        raise ValueError('Vendor assets: non-finite manifest value')

    try:
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError('Vendor assets: invalid manifest JSON') from None
    require(isinstance(data, dict) and set(data) == {'schema_version', 'files'}
            and type(data['schema_version']) is int and data['schema_version'] == 1,
            'unsupported manifest schema')
    rows = data['files']
    require(isinstance(rows, list) and len(rows) == len(DESTINATIONS),
            'manifest must cover every vendored runtime asset')
    sources, destinations, total = set(), set(), 0
    for row in rows:
        require(isinstance(row, dict)
                and set(row) == {'source', 'destination', 'sha256', 'size_bytes'},
                'invalid manifest entry')
        source, destination = row['source'], row['destination']
        path_at(root, source)
        path_at(root, destination)
        require(source.startswith(('vendor/jma/', 'vendor/natural-earth/', 'vendor/noto-cjk/')),
                'asset source must belong to a known provider')
        require(destination in DESTINATIONS, 'unexpected runtime destination')
        require(source not in sources and destination not in destinations,
                'duplicate asset source or destination')
        sources.add(source)
        destinations.add(destination)
        require(isinstance(row['sha256'], str) and re.fullmatch('[0-9a-f]{64}', row['sha256']),
                'invalid asset checksum')
        size = row['size_bytes']
        require(type(size) is int and 0 <= size <= MAX_FILE_BYTES, 'invalid asset size')
        total += size
    require(destinations == DESTINATIONS and total <= MAX_TOTAL_BYTES,
            'incomplete or oversized vendor snapshot')
    return rows, hashlib.sha256(raw).hexdigest()


def matches(path, row):
    return fingerprint(path) == (row['sha256'], row['size_bytes'])


def install(root, row):
    source, target = (path_at(root, row[key]) for key in ('source', 'destination'))
    target.parent.mkdir(parents=True, exist_ok=True)
    # Recheck parents after creating them, before opening a temporary file.
    path_at(root, row['destination'])
    descriptor, name = tempfile.mkstemp(prefix='.vendor-', dir=target.parent)
    temporary = Path(name)
    try:
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, 'wb') as writer, source.open('rb') as reader:
            while chunk := reader.read(1024 * 1024):
                size += len(chunk)
                require(size <= row['size_bytes'], 'vendor source changed while copying')
                writer.write(chunk)
                digest.update(chunk)
        require((digest.hexdigest(), size) == (row['sha256'], row['size_bytes']),
                'vendor source changed while copying')
        os.chmod(temporary, 0o644)
        path_at(root, row['destination'])
        try:
            # A hard link installs an already complete file, and fails if a
            # concurrent writer created the destination. It never overwrites.
            os.link(temporary, target)
            return True
        except FileExistsError:
            path_at(root, row['destination'])
            require(matches(target, row), 'runtime asset changed while copying: ' + row['destination'])
            return False
    finally:
        temporary.unlink(missing_ok=True)


def prepare(root=ROOT, *, check=False):
    root = Path(root).absolute()
    require(root.is_dir() and not any(path.is_symlink() for path in (root, *root.parents)),
            'expected an unlinked checkout directory')
    rows, manifest_hash = load_lock(root)
    missing = []
    # Check all sources and all present destinations before writing any asset.
    for row in rows:
        source = path_at(root, row['source'])
        require(matches(source, row), 'vendor checksum or size differs: ' + row['source'])
        target = path_at(root, row['destination'])
        if target.exists():
            require(matches(target, row), 'existing runtime asset differs: ' + row['destination'])
        else:
            missing.append(row)
    require(not check or not missing, 'runtime assets are missing; run materialization first')
    created = sum(install(root, row) for row in missing) if not check else 0
    require(load_lock(root)[1] == manifest_hash, 'vendor manifest changed during materialization')
    return {'files': len(rows), 'created': created, 'unchanged': len(rows) - created,
            'manifest_sha256': manifest_hash}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='checkout containing vendor/manifest.json')
    parser.add_argument('--check', action='store_true', help='verify vendor and runtime files without writing')
    args = parser.parse_args(argv)
    try:
        result = prepare(args.root, check=args.check)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
