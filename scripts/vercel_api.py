"""Shared Vercel source deployment and verification; no project mutation API.

The production create body's autoAssignCustomDomains=False is the same
per-deployment field sent by the official CLI's --prod --skip-domain option.
"""

import ast
import base64
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts.prepare_ci_artifact import verify_public
from scripts.prepare_release_candidate import MAX_FILE_BYTES, verify_bundle


class DeploymentError(ValueError):
    """Messages in this class are safe to publish in an Actions log."""


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None


class VercelAPI:
    def __init__(self, token, team_id):
        require(isinstance(token, str) and re.fullmatch(r'[!-~]{1,4096}', token),
                'VERCEL_TOKEN must be configured for deployment')
        require(re.fullmatch(r'team_[A-Za-z0-9]+', team_id), 'Invalid Vercel team identity')
        self.token, self.team_id = token, team_id
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, *, data=None, headers=None, expect_json=True):
        routes = {
            'GET': r'/(?:v9/projects/prj_[A-Za-z0-9]+|v13/deployments/dpl_[A-Za-z0-9]+|'
                   r'v2/deployments/dpl_[A-Za-z0-9]+/aliases|v4/aliases/[a-z0-9.-]+)',
            'POST': r'/(?:v2/files|v13/deployments|v10/projects/prj_[A-Za-z0-9]+/promote/dpl_[A-Za-z0-9]+)',
        }
        require(method in routes and re.fullmatch(routes[method], path), 'Unsupported Vercel API endpoint')
        request = Request('https://api.vercel.com' + path + '?' + urlencode({'teamId': self.team_id}),
                          method=method, data=data,
                          headers={**(headers or {}), 'Authorization': 'Bearer ' + self.token})
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read(1024 * 1024 + 1)
                require(len(raw) <= 1024 * 1024, 'Vercel API response exceeded the size limit')
                return json.loads(raw) if expect_json else None
        except HTTPError as error:
            raise DeploymentError('Vercel API request failed (HTTP ' + str(error.code) + ')') from None
        except (URLError, OSError, ValueError) as error:
            if isinstance(error, DeploymentError):
                raise
            raise DeploymentError('Vercel API request failed or returned invalid JSON') from None

    def json(self, method, path, body):
        return self.request(method, path, data=json.dumps(body).encode(),
                            headers={'Content-Type': 'application/json'})


class GitHubOIDC:
    """Fetch a short-lived token just before HTTPS checks and refresh in memory.

    GitHub's actions/toolkit core/oidc-utils.ts uses the runner-provided URL and
    bearer token. Its endpoint is distinct from the token issuer. Vercel checks
    the signature; local claim checks bind the response to this Actions job.
    """

    def __init__(self, environment):
        require(environment.get('GITHUB_ACTIONS') == 'true', 'OIDC checks must run in GitHub Actions')
        self.claims = {claim: environment.get(name, '') for claim, name in (
            ('repository', 'GITHUB_REPOSITORY'), ('sha', 'GITHUB_SHA'), ('ref', 'GITHUB_REF'),
            ('run_id', 'GITHUB_RUN_ID'), ('run_attempt', 'GITHUB_RUN_ATTEMPT'))}
        require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.claims['repository'])
                and re.fullmatch(r'[0-9a-f]{40}', self.claims['sha'])
                and self.claims['ref'].startswith('refs/')
                and all(re.fullmatch(r'[1-9][0-9]*', self.claims[k]) for k in ('run_id', 'run_attempt')),
                'Invalid GitHub OIDC job identity')
        self.audience = 'https://github.com/' + self.claims['repository'].split('/')[0]
        raw_url = environment.get('ACTIONS_ID_TOKEN_REQUEST_URL', '')
        self.request_token = environment.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')
        require(isinstance(raw_url, str) and len(raw_url) <= 8192
                and isinstance(self.request_token, str) and re.fullmatch(r'[!-~]{1,32768}', self.request_token),
                'GitHub OIDC requires id-token: write and the runner request credentials')
        try:
            url = urlsplit(raw_url)
            valid = (url.scheme == 'https' and url.port in (None, 443) and not url.username and not url.password
                     and not url.fragment and isinstance(url.hostname, str)
                     and re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.actions\.githubusercontent\.com', url.hostname)
                     and url.path.startswith('/') and not any(c in raw_url for c in ('\r', '\n', '\\')))
            require(valid, 'GitHub OIDC token endpoint must be a GitHub Actions HTTPS host')
            query = parse_qsl(url.query, keep_blank_values=True)
            require(not any(key == 'audience' for key, _ in query), 'OIDC endpoint must not override its audience')
            self.url = urlunsplit((url.scheme, url.netloc, url.path, urlencode([*query, ('audience', self.audience)]), ''))
        except ValueError as error:
            if isinstance(error, DeploymentError):
                raise
            raise DeploymentError('Invalid GitHub OIDC token endpoint') from None
        self.opener = build_opener(NoRedirect())
        self.token, self.expires = None, 0

    def __call__(self):
        now = time.time()
        if self.token is not None and self.expires > now + 60:
            return self.token
        request = Request(self.url, headers={'Authorization': 'Bearer ' + self.request_token,
                                            'Accept': 'application/json'}, method='GET')
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read(65537)
            require(len(raw) <= 65536, 'GitHub OIDC response exceeded the size limit')
            result = json.loads(raw)
            token = result.get('value') if isinstance(result, dict) else None
            require(isinstance(token, str) and len(token) <= 32768
                    and re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token),
                    'GitHub did not return a valid OIDC token')
            encoded = token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            require(isinstance(claims, dict) and claims.get('iss') == 'https://token.actions.githubusercontent.com'
                    and claims.get('aud') == self.audience
                    and all(claims.get(key) == value for key, value in self.claims.items())
                    and type(claims.get('exp')) is int and now + 60 < claims['exp'] <= now + 3600,
                    'GitHub OIDC token does not match this job or has an invalid lifetime')
        except HTTPError as error:
            raise DeploymentError('GitHub OIDC request failed (HTTP ' + str(error.code) + ')') from None
        except (URLError, OSError, ValueError, KeyError, TypeError):
            raise DeploymentError('GitHub OIDC request or token validation failed; credentials omitted') from None
        self.token, self.expires = token, claims['exp']
        return self.token


def actions_identity(environment):
    require(environment.get('GITHUB_ACTIONS') == 'true', 'Deployment must run in GitHub Actions')
    values = {key: environment.get(name, '') for key, name in (
        ('repository', 'GITHUB_REPOSITORY'), ('sha', 'GITHUB_SHA'),
        ('run_id', 'GITHUB_RUN_ID'), ('attempt', 'GITHUB_RUN_ATTEMPT'),
        ('team_id', 'VERCEL_ORG_ID'), ('project_id', 'VERCEL_PROJECT_ID'))}
    patterns = {'repository': r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', 'sha': r'[0-9a-f]{40}',
                'run_id': r'[1-9][0-9]*', 'attempt': r'[1-9][0-9]*',
                'team_id': r'team_[A-Za-z0-9]+', 'project_id': r'prj_[A-Za-z0-9]+'}
    require(all(isinstance(value, str) and re.fullmatch(patterns[key], value)
                for key, value in values.items()), 'Invalid Actions or Vercel target identity')
    return values


def check_key_support(files):
    """Inspect the versioned contract marker, without importing application code."""
    source = next((data for descriptor, data in files
                   if descriptor['file'] == 'image_service/app.py'), None)
    require(source is not None, 'Candidate does not include its image HTTP application')
    try:
        tree = ast.parse(source)
        markers = [node.value.value for node in tree.body
                   if isinstance(node, ast.Assign) and len(node.targets) == 1
                   and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == 'IMAGE_CHECK_KEYS_VERSION'
                   and isinstance(node.value, ast.Constant)]
    except (SyntaxError, ValueError):
        markers = []
    require(markers == [1] and type(markers[0]) is int,
            'Candidate lacks deployment check-key support; publish a new distribution revision')


def snapshot(bundle, artifact, source_commit):
    """Capture exactly the immutable image files already bound to CI evidence."""
    bundle, artifact = Path(bundle).absolute(), Path(artifact).absolute()
    require(not any(p.is_symlink() for root in (bundle, artifact) for p in (root, *root.parents)),
            'Linked verification directories are not allowed')
    try:
        candidate, evidence = verify_bundle(bundle), verify_public(artifact)
        require(candidate['source'] == {'commit': source_commit, 'working_tree': False}
                and evidence['source_commit'] == source_commit and evidence['source_dirty'] is False,
                'Candidate must belong to the recorded clean source commit')
        require(candidate['manifest_sha256'] == evidence['candidate_manifest_sha256'],
                'Candidate differs from the successful CI evidence')
        files = []
        for name, expected in sorted(candidate['files'].items()):
            if not name.startswith('images/'):
                continue
            source = bundle / name
            require(not any(p.is_symlink() for p in (source, *source.parents)), 'Linked candidate files are not allowed')
            with source.open('rb') as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            require(len(data) <= MAX_FILE_BYTES and hashlib.sha256(data).hexdigest() == expected,
                    'Candidate file changed after verification')
            files.append(({'file': name.removeprefix('images/'), 'sha': hashlib.sha1(data).hexdigest(),
                           'size': len(data)}, data))
        require(files and verify_bundle(bundle) == candidate and verify_public(artifact) == evidence,
                'Candidate or verification evidence changed while capturing files')
        check_key_support(files)
        return candidate, files
    except DeploymentError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise DeploymentError('Candidate or CI evidence validation failed') from None


def check_project(api, ctx):
    project = api.request('GET', '/v9/projects/' + ctx['project_id'])
    require(isinstance(project, dict) and project.get('id') == ctx['project_id']
            and project.get('accountId') == ctx['team_id'],
            'Vercel project does not belong to the requested team')
    name = project.get('name')
    require(isinstance(name, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', name),
            'Vercel returned an invalid project name')
    return name


def deployment_state(result, ctx, target, deployment_id=None):
    require(isinstance(result, dict) and result.get('projectId') == ctx['project_id']
            and result.get('target') in ((None, 'preview') if target == 'preview' else ('production',)),
            'Vercel returned an unexpected deployment target')
    identity = result.get('id')
    require(isinstance(identity, str) and re.fullmatch(r'dpl_[A-Za-z0-9]+', identity)
            and (deployment_id is None or deployment_id == identity), 'Vercel deployment identity differs')
    state = result.get('readyState')
    require(state in ('QUEUED', 'INITIALIZING', 'BUILDING', 'READY'),
            'Vercel build did not complete successfully; inspect its build logs')
    return identity, state


def stage(api, ctx, candidate, files, *, target, release_tag=None):
    require(target in ('preview', 'production'), 'Unknown deployment target')
    name = check_project(api, ctx)
    for descriptor, data in files:
        api.request('POST', '/v2/files', data=data, expect_json=False,
                    headers={'Content-Type': 'application/octet-stream', 'Content-Length': str(len(data)),
                             'x-vercel-digest': descriptor['sha']})
    # The image URL protocol permits at most 16 characters in a key ID.
    key_id, key = 'check-' + secrets.token_hex(5), secrets.token_hex(32)
    keyring = json.dumps({key_id: key}, sort_keys=True)
    metadata = {'shirasuSourceCommit': candidate['source']['commit'],
                'shirasuCandidateSha256': candidate['manifest_sha256'],
                'shirasuActionsRun': ctx['run_id'], 'shirasuPurpose': target + '-verification'}
    if release_tag is not None:
        metadata['shirasuReleaseTag'] = release_tag
    body = {'name': name, 'project': ctx['project_id'], 'files': [d for d, _ in files],
            'env': {'SHIRASU_IMAGE_CHECK_KEYS': keyring}, 'meta': metadata}
    if target == 'production':
        body.update(target='production', autoAssignCustomDomains=False)
    deployed = api.json('POST', '/v13/deployments', body)
    identity, state = deployment_state(deployed, ctx, target)
    deadline = time.monotonic() + 900
    while state != 'READY':
        require(time.monotonic() < deadline, 'Vercel build exceeded the wait limit')
        time.sleep(5)
        deployed = api.request('GET', '/v13/deployments/' + identity)
        _, state = deployment_state(deployed, ctx, target, identity)
    host = deployed.get('url')
    require(isinstance(host, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,200}\.vercel\.app', host),
            'Vercel returned an unexpected deployment URL')
    require(isinstance(deployed.get('meta'), dict)
            and all(deployed['meta'].get(k) == v for k, v in metadata.items()),
            'Vercel deployment metadata differs from the checked candidate')
    if target == 'production':
        unassigned(api, ctx, identity)
    return {'deployment_id': identity, 'deployment_url': 'https://' + host,
            'file_count': len(files)}, {'SHIRASU_IMAGE_KEYS': keyring}


def unassigned(api, ctx, identity):
    deployed = api.request('GET', '/v13/deployments/' + identity)
    _, state = deployment_state(deployed, ctx, 'production', identity)
    aliases = api.request('GET', '/v2/deployments/' + identity + '/aliases')
    require(state == 'READY' and isinstance(aliases, dict) and aliases.get('aliases') == [],
            'Production candidate must remain READY without assigned domains')


def output_paths(output, inputs):
    output = Path(output).absolute()
    paths = [output, output.with_name(output.stem + '-candidate.json'),
             output.with_name(output.stem + '-production.json')]
    for path in paths:
        require(not path.exists() and not any(p.is_symlink() for p in (path, *path.parents)),
                'Deployment reports must use new unlinked files')
        require(not any(path.resolve().is_relative_to(Path(root).resolve()) for root in inputs),
                'Deployment reports must be outside verified inputs')
    return paths


def verify_http(bundle, artifact, url, report, *, phase, environment, oidc=None):
    from scripts.check_vercel_release import check
    host = url.removeprefix('https://')
    checked = check(bundle, artifact, url, [host], report, phase=phase, environment=environment, oidc=oidc)
    require(checked.get('state') == 'success' and len(checked.get('images', [])) == 95,
            'Live HTTPS verification did not pass all 95 images')
    return {'state': 'success', 'images': 95, 'requests': len(checked['requests']),
            'report': Path(report).name, 'sha256': hashlib.sha256(Path(report).read_bytes()).hexdigest()}


def record(output, result):
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write('\n')


def failure(result, error):
    result['state'] = 'failed'
    result['failure'] = str(error) if isinstance(error, DeploymentError) else type(error).__name__
