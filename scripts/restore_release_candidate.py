#!/usr/bin/env python3
"""Restore a tested candidate from fixed GHCR digests, without running its code."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.prepare_release_candidate import CANDIDATE_VERSION, MANIFEST_NAME, MAX_MANIFEST_BYTES, verify_bundle


DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
SHA = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate release JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Non-finite release JSON value')


def read_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid release JSON') from error


def validate_receipt(receipt, manifest_bytes):
    """Validate the public receipt and return its bound candidate manifest.

    Registry manifest digests and Docker image IDs are separate identities.
    Both are required; mutable tags or other repositories are never accepted.
    """
    require(isinstance(receipt, dict)
            and type(receipt.get('schema_version')) is int and receipt['schema_version'] == 1,
            'Unsupported release receipt')
    require(receipt.get('version') == CANDIDATE_VERSION, 'Unsupported release version')
    source = receipt.get('source_commit')
    require(isinstance(source, str) and COMMIT.fullmatch(source), 'Invalid release source commit')
    digest = receipt.get('candidate_manifest_sha256')
    require(isinstance(digest, str) and SHA.fullmatch(digest), 'Invalid candidate manifest digest')
    require(isinstance(manifest_bytes, bytes) and len(manifest_bytes) <= MAX_MANIFEST_BYTES,
            'Candidate manifest exceeds the size limit')
    require(hashlib.sha256(manifest_bytes).hexdigest() == digest, 'Candidate manifest digest differs')
    manifest = read_json(manifest_bytes)
    require(isinstance(manifest, dict)
            and type(manifest.get('schema_version')) is int and manifest['schema_version'] == 1
            and manifest.get('candidate_version') == receipt['version'],
            'Candidate manifest version differs')
    require(manifest.get('source') == {'commit': source, 'working_tree': False},
            'Candidate must come from the recorded clean source commit')
    run = receipt.get('run')
    require(isinstance(run, dict), 'Missing Actions run identity')
    repository = run.get('repository')
    require(isinstance(repository, str) and len(repository) <= 256
            and REPOSITORY.fullmatch(repository), 'Invalid Actions repository')
    for field in ('id', 'attempt'):
        value = run.get(field)
        require(isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value),
                'Invalid Actions run ' + field)
    images = receipt.get('images')
    require(isinstance(images, dict) and set(images) == {'receiver', 'renderer'},
            'Release must contain receiver and renderer images')
    for role, suffix in (('receiver', 'receiver'), ('renderer', 'images')):
        image = images[role]
        require(isinstance(image, dict), 'Invalid release image')
        image_id, reference = image.get('image_id'), image.get('reference')
        require(isinstance(image_id, str) and DIGEST.fullmatch(image_id), 'Invalid Docker image ID')
        prefix = 'ghcr.io/' + repository.lower() + '-' + suffix + '@'
        require(isinstance(reference, str) and reference.startswith(prefix)
                and DIGEST.fullmatch(reference[len(prefix):]),
                'Image reference must use the expected GHCR package and a fixed digest')
    return manifest


def docker(arguments):
    result = subprocess.run(['docker', *arguments], capture_output=True, text=True,
                            timeout=600, check=False)
    require(result.returncode == 0,
            'Docker ' + arguments[0] + ' failed; check registry access and the saved digest')
    return result.stdout.strip()


def restore(release, manifest, output):
    output = Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'Restore destination already exists')
    release, manifest = Path(release), Path(manifest)
    for path in (release, manifest):
        require(path.is_file() and not path.is_symlink(), 'Expected a regular release metadata file')
        require(path.stat().st_size <= MAX_MANIFEST_BYTES, 'Release metadata exceeds the size limit')
    receipt, raw = read_json(release.read_bytes()), manifest.read_bytes()
    validate_receipt(receipt, raw)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.restore-release-', dir=output.parent))
    try:
        for role, package in (('receiver', 'receiver'), ('renderer', 'images')):
            image = receipt['images'][role]
            docker(['pull', image['reference']])
            image_id = docker(['image', 'inspect', '--format', '{{.Id}}', image['reference']])
            require(image_id == image['image_id'], 'Pulled image ID differs from the tested image')
            container = docker(['create', '--network', 'none', '--read-only',
                                '--entrypoint', '/bin/true', image_id])
            require(re.fullmatch(r'[0-9a-f]{64}', container), 'Docker returned an invalid container ID')
            try:
                destination = stage / package
                destination.mkdir()
                docker(['cp', container + ':/app/.', str(destination)])
            finally:
                docker(['rm', '--volumes', container])
        (stage / MANIFEST_NAME).write_bytes(raw)
        checked = verify_bundle(stage)
        require(checked['manifest_sha256'] == receipt['candidate_manifest_sha256'],
                'Restored candidate differs from the released candidate')
        require(not output.exists() and not output.is_symlink(), 'Restore destination now exists')
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'directory': str(output), 'candidate_manifest_sha256': checked['manifest_sha256'],
            'source_commit': receipt['source_commit'], 'images': receipt['images']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = restore(args.release, args.manifest, args.output)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Release restore failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
