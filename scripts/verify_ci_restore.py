#!/usr/bin/env python3
"""Verify a stored release on another runner before advancing its latest tags."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts import publish_ci_release as publication
from scripts.prepare_release_candidate import MAX_MANIFEST_BYTES, verify_bundle
from scripts.prepare_release_compose import prepare as prepare_compose
from scripts.restore_release_candidate import read_json, restore, validate_receipt
from scripts.release_identity import receipt_tag


def require(condition, message):
    if not condition:
        raise ValueError(message)


def metadata_bytes(path):
    path = Path(path).absolute()
    require(path.is_file() and not path.is_symlink()
            and not any(parent.is_symlink() for parent in path.parents),
            'Expected unlinked release metadata')
    with path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    require(len(raw) <= MAX_MANIFEST_BYTES, 'Release metadata exceeds the size limit')
    return raw


def current_release(repository, tag, source):
    """A retry of an older release must not move latest backwards."""
    release = publication.api('repos/' + repository + '/releases/tags/' + tag)
    # GitHub does not use target_commitish when the Git tag already exists;
    # it may still report a branch name. Resolve the actual tag below instead.
    require(isinstance(release, dict) and release.get('tag_name') == tag
            and release.get('draft') is False and release.get('prerelease') is False
            and type(release.get('id')) is int and release['id'] > 0,
            'Stored release does not identify the published version')
    latest = publication.api('repos/' + repository + '/releases/latest')
    require(isinstance(latest, dict) and latest.get('id') == release['id']
            and latest.get('tag_name') == tag,
            'Only the latest published release may advance the latest image tags')
    require(publication.resolve_tag_commit(repository, tag) == source,
            'Published Git tag differs from the recorded source commit')
    return release


def verify_anonymous_pull(images):
    """Prove the fixed public images can be pulled with an empty client config."""
    with tempfile.TemporaryDirectory(prefix='shirasu-anonymous-ghcr-') as directory:
        for image in images.values():
            publication.run(['docker', '--config', directory, 'pull', image['reference']])
            actual = publication.run(['docker', 'image', 'inspect', '--format', '{{.Id}}',
                                      image['reference']]).strip()
            require(actual == image['image_id'], 'Anonymous pull returned a different image ID')


def verify(release_path, manifest_path, output):
    ctx = publication.context(os.environ)
    output = Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'Verification output already exists')
    require(not any(parent.is_symlink() for parent in output.parents),
            'Linked verification output is not allowed')
    release_path, manifest_path = Path(release_path), Path(manifest_path)
    receipt_raw, manifest_raw = metadata_bytes(release_path), metadata_bytes(manifest_path)
    receipt = read_json(receipt_raw)
    manifest = validate_receipt(receipt, manifest_raw)
    require(receipt['source_commit'] == ctx['sha']
            and receipt['run']['repository'].lower() == ctx['repository'].lower()
            and receipt['run']['id'] == ctx['id']
            and int(receipt['run']['attempt']) <= int(ctx['attempt']),
            'Stored release must belong to this Actions run and source commit')
    tag = receipt_tag(receipt)
    require(ctx.get('tag') in (None, tag), 'Actions Git tag differs from the candidate version')
    repository = ctx['repository']
    published = current_release(repository, tag, ctx['sha'])
    repo = publication.api('repos/' + repository)
    require(isinstance(repo, dict) and repo.get('full_name', '').lower() == repository.lower()
            and repo.get('owner', {}).get('type') in ('User', 'Organization'),
            'Unexpected GitHub repository')

    output.mkdir(parents=True)
    status_path = output / 'restore-status.json'
    stage, completed_aliases = 'restore', {}

    def status(state):
        status_path.write_text(json.dumps({
            'schema_version': 1, 'state': state, 'stage': stage,
            'source_commit': ctx['sha'], 'run_id': ctx['id'],
            'completed_aliases': completed_aliases,
        }, sort_keys=True, indent=2) + '\n', encoding='utf-8')

    status('running')
    try:
        verify_anonymous_pull(receipt['images'])
        restored = restore(release_path, manifest_path, output / 'candidate')
        require(restored['candidate_manifest_sha256'] == receipt['candidate_manifest_sha256']
                and restored['source_commit'] == ctx['sha']
                and restored['images'] == receipt['images'], 'Restored release identity differs')
        checked = verify_bundle(output / 'candidate')
        require(checked == dict(manifest, manifest_sha256=receipt['candidate_manifest_sha256']),
                'Restored files differ from the stored manifest')
        # The input files must remain the exact bytes downloaded from the release.
        require(metadata_bytes(release_path) == receipt_raw
                and metadata_bytes(manifest_path) == manifest_raw,
                'Release metadata changed during restoration')
        stage = 'compose'
        status('running')
        compose = prepare_compose(output / 'candidate', receipt['images']['receiver']['image_id'],
                                  output / 'compose.release.yaml')
        require(compose['bundle_manifest_sha256'] == receipt['candidate_manifest_sha256']
                and compose['image_id'] == receipt['images']['receiver']['image_id'],
                'Restored receiver Compose verification differs')
        require(verify_bundle(output / 'candidate') == checked,
                'Restored candidate changed during Compose verification')

        stage = 'latest'
        status('running')
        package_name = repository.lower().split('/')[1]
        for suffix in ('receiver', 'images'):
            publication.ensure_public(repository, repo['owner']['type'], package_name + '-' + suffix,
                                       ctx['id'] + '-' + ctx['attempt'])
        require(current_release(repository, tag, ctx['sha'])['id'] == published['id'],
                'Published release changed during verification')
        for role, image in receipt['images'].items():
            require(publication.tag_image(image, 'latest') == image,
                    'Latest image alias differs from the restored digest')
            completed_aliases[role] = image['reference'].split('@', 1)[0] + ':latest'
            status('running')

        result = {
            'schema_version': 1, 'mode': 'ci-restore', 'version': receipt['version'],
            'source_commit': ctx['sha'], 'release_tag': tag,
            'platform': receipt.get('platform'),
            'candidate_manifest_sha256': receipt['candidate_manifest_sha256'],
            'run': {key: ctx[key] for key in ('repository', 'id', 'attempt')},
            'publication_run': {key: receipt['run'][key] for key in ('repository', 'id', 'attempt')},
            'images': {role: {key: image[key] for key in ('reference', 'image_id')}
                       for role, image in receipt['images'].items()},
            'file_count': len(checked['files']), 'aliases': completed_aliases,
            'checks': {'release_metadata_matches_run': True, 'fixed_digest_pull_and_image_ids': True,
                       'all_candidate_files_match': True, 'receiver_startup_and_compose': True,
                       'anonymous_digest_pull': True, 'public_linked_packages': True,
                       'latest_release_guard': True,
                       'latest_aliases_match': True},
        }
        with (output / 'restore-result.json').open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(result, sort_keys=True, indent=2) + '\n')
        stage = 'complete'
        status('success')
        return result
    except Exception:
        # Keep successful extraction and its status for diagnosis. A retry uses
        # a fresh runner/output rather than overwriting evidence from this one.
        status('failed')
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify(args.release, args.manifest, args.output)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('Stored release verification failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
