#!/usr/bin/env python3
"""Restore a published GHCR release and its checked HTTP corpus for Vercel."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from tools.release.prepare_ci_artifact import verify_public
from tools.release.prepare_release_candidate import MAX_MANIFEST_BYTES, verify_bundle
from tools.release.release_identity import RELEASE_TAG_RE, receipt_tag
from tools.release.restore_release_candidate import read_json, restore, validate_receipt
from tools.release.verify_ci_restore import current_release


RESTORE_CHECKS = ('release_metadata_matches_run', 'fixed_digest_pull_and_image_ids',
                  'all_candidate_files_match', 'receiver_startup_and_compose',
                  'anonymous_digest_pull', 'public_linked_packages',
                  'latest_release_guard', 'latest_aliases_match')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_metadata(path):
    path = Path(path).absolute()
    require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
            'Expected an unlinked release metadata file')
    with path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    require(len(raw) <= MAX_MANIFEST_BYTES, 'Release metadata is too large')
    return raw


def validate_metadata(directory, tag, repository):
    require(isinstance(tag, str) and RELEASE_TAG_RE.fullmatch(tag), 'Invalid release tag')
    receipt = read_json(read_metadata(directory / 'release.json'))
    manifest = validate_receipt(receipt, read_metadata(directory / 'candidate-manifest.json'))
    result = read_json(read_metadata(directory / 'restore-result.json'))
    require(receipt_tag(receipt) == tag and receipt['run']['repository'] == repository,
            'Release tag or repository differs from the requested release')
    require(isinstance(result, dict) and type(result.get('schema_version')) is int
            and result['schema_version'] == 1
            and result.get('mode') == 'ci-restore', 'Missing successful separate-runner restore report')
    for name in ('version', 'source_commit', 'release_tag', 'platform',
                 'candidate_manifest_sha256', 'images'):
        require(result.get(name) == receipt.get(name), 'Restore report differs from release metadata: ' + name)
    checks = result.get('checks')
    require(result.get('publication_run') == receipt['run']
            and type(result.get('file_count')) is int
            and result.get('file_count') == len(manifest['files'])
            and isinstance(checks, dict)
            and all(checks.get(name) is True for name in RESTORE_CHECKS),
            'Published release has not passed all required restoration checks')
    return receipt


def extract_public(archive, destination):
    """Unpack only regular, bounded files; never links or arbitrary tar paths."""
    archive, destination = Path(archive).absolute(), Path(destination).absolute()
    require(archive.is_file() and not any(p.is_symlink() for p in (archive, *archive.parents))
            and archive.stat().st_size <= 128 * 1024 * 1024, 'Invalid public verification archive')
    require(not destination.exists() and not any(p.is_symlink() for p in (destination, *destination.parents)),
            'Expected a new unlinked public verification output')
    with tarfile.open(archive, 'r:gz') as stream:
        entries = stream.getmembers()
        require(0 < len(entries) <= 4096, 'Unexpected public verification file count')
        names, total = set(), 0
        for entry in entries:
            path = PurePosixPath(entry.name)
            require(entry.isfile() and not path.is_absolute()
                    and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,511}', entry.name)
                    and all(p not in ('', '.', '..') for p in entry.name.split('/'))
                    and entry.name not in names and 0 <= entry.size <= 32 * 1024 * 1024,
                    'Unsafe public verification archive entry')
            names.add(entry.name)
            total += entry.size
        require(total <= 256 * 1024 * 1024, 'Public verification archive is too large')
        destination.mkdir(parents=True)
        for entry in entries:
            path = destination / entry.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(entry) as source, path.open('xb') as target:
                shutil.copyfileobj(source, target)
    return verify_public(destination)


def prepare(metadata, output, tag, *, environment=None):
    env = os.environ if environment is None else environment
    repository = env.get('GITHUB_REPOSITORY', '')
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'Invalid Actions repository')
    ref, event = env.get('GITHUB_REF'), env.get('GITHUB_EVENT_NAME')
    require(env.get('GITHUB_ACTIONS') == 'true'
            and ((ref == 'refs/heads/main' and event == 'workflow_dispatch')
                 or (ref == 'refs/tags/' + tag and event == 'push')),
            'Vercel releases require a formal tag push or manual main run')
    metadata, output = Path(metadata).absolute(), Path(output).absolute()
    require(not output.exists() and not any(p.is_symlink() for p in (output, *output.parents)),
            'Expected a new unlinked restore output directory')
    receipt = validate_metadata(metadata, tag, repository)
    if event == 'push':
        require(receipt['source_commit'] == env.get('GITHUB_SHA'), 'Tag push differs from release source')
    # A manual run uses current workflow code, while the saved source commit and
    # immutable registry digests identify the release to deploy.
    current_release(repository, tag, receipt['source_commit'])
    ancestry = subprocess.run(['git', 'merge-base', '--is-ancestor', receipt['source_commit'],
                              'refs/remotes/origin/main'], cwd=ROOT, check=False,
                             capture_output=True, timeout=30)
    require(ancestry.returncode == 0, 'Release source is not in the fetched main history')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.vercel-release-', dir=output.parent))
    try:
        # No local registry credentials are needed for these public packages.
        with tempfile.TemporaryDirectory(prefix='shirasu-anonymous-ghcr-') as config:
            original = os.environ.get('DOCKER_CONFIG')
            os.environ['DOCKER_CONFIG'] = config
            try:
                restore(metadata / 'release.json', metadata / 'candidate-manifest.json', stage / 'candidate')
            finally:
                if original is None:
                    os.environ.pop('DOCKER_CONFIG', None)
                else:
                    os.environ['DOCKER_CONFIG'] = original
        evidence = extract_public(metadata / 'public-verification.tar.gz', stage / 'public')
        require(evidence['candidate_manifest_sha256'] == receipt['candidate_manifest_sha256']
                and evidence['source_commit'] == receipt['source_commit']
                and evidence['source_dirty'] is False,
                'Public HTTP evidence does not match the restored release')
        require(verify_bundle(stage / 'candidate')['manifest_sha256'] == receipt['candidate_manifest_sha256'],
                'Restored candidate changed while preparing Vercel input')
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'release_tag': tag, 'source_commit': receipt['source_commit'],
            'candidate_manifest_sha256': receipt['candidate_manifest_sha256'],
            'directory': str(output)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--release-tag', required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.metadata, args.output, args.release_tag)
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError):
        print('Vercel release preparation failed; verify the fixed release and restore report', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
