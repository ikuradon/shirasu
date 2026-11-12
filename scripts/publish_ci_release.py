#!/usr/bin/env python3
"""Keep the images tested by a release Actions run in private GHCR packages."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.prepare_ci_artifact import verify_public
from scripts.prepare_release_candidate import verify_bundle
from scripts.prepare_release_compose import inspect_image
from scripts.restore_release_candidate import validate_receipt


IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')
VERIFY_FILES = '''import hashlib,json,pathlib,sys
expected=json.load(sys.stdin); actual={}
for p in pathlib.Path('/app').rglob('*'):
 if p.is_symlink(): raise SystemExit('Linked image source')
 if p.is_dir(): continue
 name=p.relative_to('/app').as_posix()
 if not p.is_file() or name not in expected: raise SystemExit('Unexpected image source')
 actual[name]=hashlib.sha256(p.read_bytes()).hexdigest()
if actual != expected: raise SystemExit('Image source differs')
print('verified')
'''


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(arguments, *, input=None, cwd=None):
    result = subprocess.run(arguments, input=input, cwd=cwd, text=True,
                            capture_output=True, timeout=900, check=False)
    require(result.returncode == 0, arguments[0] + ' failed; release was not completed')
    return result.stdout


def api(path, *, method='GET', body=None, missing=False):
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN', '')
    require(bool(token), 'GH_TOKEN is required inside the release workflow')
    data = None if body is None else json.dumps(body).encode()
    request = Request('https://api.github.com/' + path, method=method, data=data,
                      headers={'Authorization': 'Bearer ' + token,
                               'Accept': 'application/vnd.github+json',
                               'Content-Type': 'application/json',
                               'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as error:
        if missing and error.code == 404:
            return None
        raise ValueError('GitHub API request failed (HTTP ' + str(error.code) + ')') from None


def context(environment):
    event, ref = environment.get('GITHUB_EVENT_NAME'), environment.get('GITHUB_REF', '')
    tag = ref.removeprefix('refs/tags/') if ref.startswith('refs/tags/') else None
    manual = event == 'workflow_dispatch' and ref == 'refs/heads/main'
    tagged = event == 'push' and tag is not None and re.fullmatch(r'images-v[1-9][0-9]*', tag)
    require(environment.get('GITHUB_ACTIONS') == 'true' and (manual or tagged),
            'Releases require a manual run on main or an images-vN tag push')
    repository = environment.get('GITHUB_REPOSITORY', '')
    sha = environment.get('GITHUB_SHA', '')
    run_id, attempt = environment.get('GITHUB_RUN_ID', ''), environment.get('GITHUB_RUN_ATTEMPT', '')
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) is not None
            and re.fullmatch(r'[0-9a-f]{40}', sha) is not None
            and re.fullmatch(r'[1-9][0-9]*', run_id) is not None
            and re.fullmatch(r'[1-9][0-9]*', attempt) is not None, 'Invalid Actions source identity')
    return {'repository': repository, 'sha': sha, 'id': run_id, 'attempt': attempt,
            'tag': tag if tagged else None}


def package_endpoint(repository, owner_type, package):
    prefix = 'orgs' if owner_type == 'Organization' else 'users'
    return prefix + '/' + repository.split('/')[0] + '/packages/container/' + quote(package, safe='')


def ensure_private(repository, owner_type, package, run_id):
    owner = repository.split('/')[0]
    endpoint = package_endpoint(repository, owner_type, package)
    info = api(endpoint, missing=True)
    if info is None:
        # Establish and inspect package visibility using an empty image FIRST.
        # No source, GIS, credentials or other payload can be exposed here.
        bootstrap = 'ghcr.io/' + owner.lower() + '/' + package + ':bootstrap-' + run_id
        with tempfile.TemporaryDirectory(prefix='shirasu-ghcr-bootstrap-') as directory:
            Path(directory, 'Dockerfile').write_text(
                'FROM scratch\nLABEL org.opencontainers.image.source="https://github.com/'
                + repository + '"\n')
            run(['docker', 'build', '--network', 'none', '--tag', bootstrap, directory])
        run(['docker', 'push', bootstrap])
        # A just-created package may not appear immediately in the Packages API.
        # Only an actual 404 can be retried; permission failures still stop here.
        for attempt in range(5):
            info = api(endpoint, missing=True)
            if info is not None:
                break
            if attempt < 4:
                time.sleep(1)
    require(isinstance(info, dict) and info.get('visibility') == 'private',
            'GHCR package must be private before uploading release payloads: ' + package)
    linked = (info.get('repository') or {}).get('full_name', '')
    require(linked.lower() == repository.lower(), 'GHCR package must be linked to this repository: ' + package)


def require_unused_image_tag(repository, owner_type, package, tag):
    """A fixed container tag must not replace a version left outside a Release."""
    page = 1
    while True:
        versions = api(package_endpoint(repository, owner_type, package)
                       + '/versions?per_page=100&page=' + str(page))
        require(isinstance(versions, list), 'Cannot check existing container tags')
        for version in versions:
            metadata = version.get('metadata') if isinstance(version, dict) else None
            container = metadata.get('container') if isinstance(metadata, dict) else None
            tags = container.get('tags') if isinstance(container, dict) else None
            require(isinstance(tags, list) and all(isinstance(value, str) for value in tags),
                    'Cannot check existing container tags')
            require(tag not in tags, 'Fixed container tag already exists: ' + package + ':' + tag)
        if len(versions) < 100:
            return
        page += 1


def push_image(image_id, name, tag):
    reference = name + ':' + tag
    run(['docker', 'tag', image_id, reference])
    pushed = run(['docker', 'push', reference])
    matches = re.findall(r'digest:\s*(sha256:[0-9a-f]{64})', pushed)
    require(len(set(matches)) == 1, 'Registry did not return one image digest')
    pinned = name + '@' + matches[0]
    run(['docker', 'pull', pinned])
    require(run(['docker', 'image', 'inspect', '--format', '{{.Id}}', pinned]).strip() == image_id,
            'Pulled registry image differs from the tested image')
    return {'reference': pinned, 'image_id': image_id}


def tag_image(image, tag):
    """Add an alias only when pushing preserves both recorded image identities."""
    require(isinstance(image, dict), 'Expected a recorded image')
    reference, image_id = image.get('reference'), image.get('image_id')
    require(isinstance(reference, str) and re.fullmatch(
        r'ghcr\.io/[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*@sha256:[0-9a-f]{64}', reference),
        'Expected a fixed GHCR image reference')
    require(isinstance(image_id, str) and IMAGE_ID.fullmatch(image_id), 'Expected a full image ID')
    require(isinstance(tag, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag),
            'Invalid container tag')
    result = push_image(image_id, reference.split('@', 1)[0], tag)
    require(result == {'reference': reference, 'image_id': image_id},
            'Container tag differs from the recorded image digest or ID')
    return result


def resolve_tag_commit(repository, tag):
    """Resolve a lightweight or at most five nested annotated release tags."""
    require(isinstance(tag, str) and re.fullmatch(r'images-v[1-9][0-9]*', tag),
            'Invalid release tag')
    ref = api('repos/' + repository + '/git/ref/tags/' + tag, missing=True)
    require(isinstance(ref, dict), 'Release tag is missing')
    target = ref.get('object')
    for depth in range(6):
        require(isinstance(target, dict) and isinstance(target.get('sha'), str)
                and re.fullmatch(r'[0-9a-f]{40}', target['sha']), 'Invalid release tag target')
        if target.get('type') == 'commit':
            return target['sha']
        require(target.get('type') == 'tag' and depth < 5, 'Unsupported release tag target or depth')
        annotation = api('repos/' + repository + '/git/tags/' + target['sha'])
        target = annotation.get('object') if isinstance(annotation, dict) else None
    raise ValueError('Release tag did not resolve to a commit')


def require_new_version(repository, tag, source_commit=None):
    if source_commit is None:
        require(api('repos/' + repository + '/git/ref/tags/' + tag, missing=True) is None,
                'This renderer version already has a release/tag; do not overwrite published URLs')
    else:
        require(resolve_tag_commit(repository, tag) == source_commit,
                'Release tag points to a different source commit')
    require(api('repos/' + repository + '/releases/tags/' + tag, missing=True) is None,
            'This renderer version already has a release/tag; do not overwrite published URLs')
    # Drafts may not have created their Git tag yet. Treat a failed draft as a
    # reserved version too, rather than silently replacing its intended build.
    page = 1
    while True:
        releases = api('repos/' + repository + '/releases?per_page=100&page=' + str(page))
        require(isinstance(releases, list), 'Cannot check existing release drafts')
        require(not any(row.get('tag_name') == tag for row in releases),
                'This renderer version has a reserved release draft')
        if len(releases) < 100:
            return
        page += 1


def publish(check_root, receiver_image, renderer_image):
    ctx = context(os.environ)
    root = Path(check_root).absolute()
    bundle = root / 'candidate'
    public = root / 'public'
    manifest = verify_bundle(bundle)
    evidence = verify_public(public)
    tag = 'images-' + manifest['candidate_version']
    require(ctx['tag'] in (None, tag), 'Release tag differs from the candidate version')
    require(manifest['source'] == {'commit': ctx['sha'], 'working_tree': False},
            'Only the clean Actions checkout can become a formal release')
    require(evidence['candidate_manifest_sha256'] == manifest['manifest_sha256'],
            'Public evidence refers to a different candidate')
    require((public / 'candidate-manifest.json').read_bytes()
            == (bundle / 'candidate-manifest.json').read_bytes(), 'Public candidate manifest differs')
    receiver_id = inspect_image(receiver_image)
    renderer_id = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', renderer_image]).strip()
    require(IMAGE_ID.fullmatch(renderer_id) is not None, 'Expected a full renderer image ID')
    compose = json.loads((root / 'results/compose.json').read_text())
    require(compose['image_id'] == receiver_id, 'Receiver differs from the tested Compose image')
    for package, image_id in [('receiver', receiver_id), ('images', renderer_id)]:
        expected = {name.split('/', 1)[1]: value for name, value in manifest['files'].items()
                    if name.startswith(package + '/')}
        result = run(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
                      '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint',
                      'python', '-i', image_id, '-B', '-c', VERIFY_FILES], input=json.dumps(expected))
        require(result.strip() == 'verified', 'Image application files differ from the candidate')

    repository = ctx['repository']
    require_new_version(repository, tag, ctx['sha'] if ctx['tag'] else None)
    if ctx['tag']:
        comparison = api('repos/' + repository + '/compare/' + ctx['sha'] + '...main')
        require(isinstance(comparison, dict) and comparison.get('status') in ('ahead', 'identical'),
                'Tagged release source must belong to main')
    repo = api('repos/' + repository)
    require(repo.get('full_name', '').lower() == repository.lower(), 'Unexpected GitHub repository')
    owner, name = repository.lower().split('/')
    packages = {'receiver': name + '-receiver', 'renderer': name + '-images'}
    for package in packages.values():
        ensure_private(repository, repo['owner']['type'], package, ctx['id'] + '-' + ctx['attempt'])
        require_unused_image_tag(repository, repo['owner']['type'], package, tag)

    # Reserve the version before pushing payloads. Keep a failed draft visible;
    # retries must not silently substitute another build under the same version.
    draft = api('repos/' + repository + '/releases', method='POST', body={
        'tag_name': tag, 'target_commitish': ctx['sha'], 'name': tag, 'draft': True,
        'body': 'Verified candidate from Actions run ' + ctx['id'] + '. '
                'Image service and receiver production switching are separate operations.'})
    image_tag = 'run-' + ctx['id'] + '-' + ctx['attempt']
    pinned = {role: push_image(image_id, 'ghcr.io/' + owner + '/' + packages[role], image_tag)
              for role, image_id in [('receiver', receiver_id), ('renderer', renderer_id)]}
    for image in pinned.values():
        tag_image(image, tag)
    for package in packages.values():
        ensure_private(repository, repo['owner']['type'], package, ctx['id'] + '-' + ctx['attempt'])
    require(verify_bundle(bundle) == manifest and verify_public(public) == evidence,
            'Candidate or public evidence changed during upload')
    receipt = {'schema_version': 1, 'version': manifest['candidate_version'],
               'source_commit': ctx['sha'], 'candidate_manifest_sha256': manifest['manifest_sha256'],
               'run': {key: ctx[key] for key in ('repository', 'id', 'attempt')}, 'images': pinned}
    validate_receipt(receipt, (bundle / 'candidate-manifest.json').read_bytes())
    with tempfile.TemporaryDirectory(prefix='shirasu-release-metadata-') as directory:
        receipt_file = Path(directory) / 'release.json'
        receipt_file.write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
        archive = Path(directory) / 'public-verification.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            # verify_public has already checked all files, links and hashes.
            for path in sorted(public.rglob('*')):
                if path.is_file():
                    output.add(path, arcname=path.relative_to(public).as_posix(), recursive=False)
        run(['gh', 'release', 'upload', tag, '--repo', repository, str(receipt_file),
             str(public / 'candidate-manifest.json'), str(archive)])
    api('repos/' + repository + '/releases/' + str(draft['id']), method='PATCH', body={'draft': False})
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-root', required=True, type=Path)
    parser.add_argument('--receiver-image', required=True)
    parser.add_argument('--renderer-image', required=True)
    args = parser.parse_args(argv)
    try:
        receipt = publish(args.check_root, args.receiver_image, args.renderer_image)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('CI release failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
