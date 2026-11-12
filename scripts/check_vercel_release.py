#!/usr/bin/env python3
"""Check a fixed release over HTTPS without recording signing keys or URLs."""

import argparse
from contextlib import contextmanager
import hashlib
import http.client
from http.cookies import CookieError, SimpleCookie
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.prepare_ci_artifact import read, verify_public
from scripts.prepare_release_candidate import verify_bundle

MAX_BODY = 8 * 1024 * 1024
FIXTURE_ORIGIN = 'https://images.example.test'
FIXTURE_KEYS = {'rehearsal': bytes(range(32))}


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def origin(base_url, allowed_hosts):
    try:
        parsed = urlsplit(base_url)
        valid = (parsed.scheme == 'https' and parsed.hostname in allowed_hosts
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
                 and parsed.netloc == parsed.hostname + (':443' if parsed.port else ''))
    except ValueError:
        valid = False
    require(valid, 'Expected an HTTPS origin on an explicitly allowed host')
    return parsed.hostname


@contextmanager
def protocol_from_bundle(bundle):
    """Load both dependency-free signing modules from the verified candidate."""
    def module(name, filename):
        spec = importlib.util.spec_from_file_location(name, bundle / 'images' / filename)
        value = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(value)
        return value

    previous = sys.modules.get('image_versions')
    try:
        sys.modules['image_versions'] = module('_vercel_release_versions', 'image_versions.py')
        yield module('_vercel_release_protocol', 'image_protocol.py')
    finally:
        if previous is None:
            sys.modules.pop('image_versions', None)
        else:
            sys.modules['image_versions'] = previous


def signing_key(protocol, environment, requested=None):
    try:
        keys = protocol.load_keys(environment.get('SHIRASU_IMAGE_KEYS', ''))
    except (TypeError, ValueError):
        raise VerificationError('Valid SHIRASU_IMAGE_KEYS is required') from None
    require(requested in keys if requested else len(keys) == 1,
            'Select a configured key with --key-id when more than one key exists')
    key_id = requested or next(iter(keys))
    require(keys[key_id] != FIXTURE_KEYS['rehearsal'], 'Public fixture keys cannot authorize live checks')
    return key_id, keys[key_id]


def directives(value):
    return {part.strip().lower() for part in value.split(',')}


def public_cache(headers):
    expected = {'public', 'max-age=31536000', 'immutable'}
    require(directives(headers.get('cache-control', '')) == expected,
            'Public browser cache policy differs')
    if 'vercel-cdn-cache-control' in headers:
        require(directives(headers['vercel-cdn-cache-control']) == {'public', 'max-age=31536000'},
                'CDN cache policy differs')


def safe_headers(headers):
    """Keep only constrained diagnostic values, never arbitrary response text."""
    patterns = {
        'x-vercel-cache': r'[A-Z_]{1,32}', 'age': r'[0-9]{1,12}',
        'x-vercel-id': r'[A-Za-z0-9:_.-]{1,128}',
        'content-type': r'image/(?:png|webp)|text/plain(?:; charset=utf-8)?',
        'content-length': r'[0-9]{1,12}', 'etag': r'"[0-9a-f]{64}"',
        'cache-control': r'[a-z0-9=, -]{1,120}',
        'vercel-cdn-cache-control': r'[a-z0-9=, -]{1,120}',
    }
    return {name: value for name, value in headers.items()
            if name in patterns and re.fullmatch(patterns[name], value)}


class HTTPS:
    def __init__(self, hostname, *, bypass=None, oidc=None, timeout=120):
        self.hostname, self.bypass, self.timeout = hostname, bypass, timeout
        require(not (bypass and oidc), 'Only one preview authentication method is allowed')
        self.oidc = oidc
        self.cookie = None
        self.trace = []

    def bootstrap_share(self, token):
        """Exchange a short-lived share token for a same-host, memory-only cookie."""
        require(not self.bypass and not self.oidc and self.cookie is None, 'Only one preview authentication method is allowed')
        require(isinstance(token, str) and 0 < len(token) <= 8192 and '\r' not in token and '\n' not in token,
                'Invalid preview share token')
        connection = http.client.HTTPSConnection(self.hostname, timeout=self.timeout,
                                                  context=ssl.create_default_context())
        status = None
        try:
            connection.request('GET', '/healthz?' + urlencode({'_vercel_share': token}),
                               headers={'Accept-Encoding': 'identity', 'User-Agent': 'shirasu-release-check/1'})
            response = connection.getresponse()
            status = response.status
            pairs = response.getheaders()
            require(len(response.read(MAX_BODY + 1)) <= MAX_BODY, 'Share response exceeds the size limit')
            require(status in (302, 307), 'Share authentication did not return the expected redirect')
            locations = [value for name, value in pairs if name.lower() == 'location']
            allowed = ('/healthz', 'https://' + self.hostname + '/healthz',
                       'https://' + self.hostname + ':443/healthz')
            require(len(locations) == 1 and locations[0] in allowed,
                    'Share authentication redirect must remain on the same host and health path without a query')
            cookies = []
            for name, value in pairs:
                if name.lower() == 'set-cookie':
                    parsed = SimpleCookie()
                    parsed.load(value)
                    if '_vercel_jwt' in parsed:
                        cookies.append(parsed['_vercel_jwt'].value)
            require(len(cookies) == 1 and re.fullmatch(r'[A-Za-z0-9._~-]{1,16384}', cookies[0]),
                    'Share authentication did not provide one valid preview cookie')
            self.cookie = '_vercel_jwt=' + cookies[0]
        except (OSError, http.client.HTTPException, CookieError):
            raise VerificationError('Share authentication failed; credentials and URL omitted') from None
        finally:
            connection.close()
            # Deliberately omit even the query hash, headers and response body.
            self.trace.append({'label': 'share-bootstrap', 'status': status})

    def request(self, path, *, label, method='GET', etag=None, accept=None, query='', revalidate=False):
        require(path.startswith('/') and not path.startswith('//') and '\r' not in path and '\n' not in path,
                'Invalid request path')
        headers = {'Accept-Encoding': 'identity', 'User-Agent': 'shirasu-release-check/1'}
        if self.bypass:
            headers['x-vercel-protection-bypass'] = self.bypass
        if self.oidc:
            token = self.oidc()
            require(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,32768}', token),
                    'Invalid GitHub OIDC token')
            headers['x-vercel-trusted-oidc-idp-token'] = token
        if self.cookie:
            headers['Cookie'] = self.cookie
        if etag is not None:
            headers['If-None-Match'] = etag
        if accept is not None:
            headers['Accept'] = accept
        if revalidate:
            headers.update({'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
        target = path + ('?' + query if query else '')
        started = time.monotonic()
        connection = http.client.HTTPSConnection(self.hostname, timeout=self.timeout,
                                                  context=ssl.create_default_context())
        try:
            connection.request(method, target, headers=headers)
            response = connection.getresponse()
            values = {}
            for name, value in response.getheaders():
                name = name.lower()
                require(name not in values or name not in ('content-length', 'content-type', 'etag'),
                        'Duplicate image response headers')
                values[name] = values[name] + ', ' + value if name in values else value
            body = response.read(MAX_BODY + 1)
            require(len(body) <= MAX_BODY, 'Image response exceeds the size limit')
            result = {'status': response.status, 'headers': values, 'body': body}
        except (OSError, http.client.HTTPException):
            self.trace.append({'label': label, 'method': method, 'status': None,
                               'path_sha256': hashlib.sha256(target.encode()).hexdigest(),
                               'duration_ms': round((time.monotonic() - started) * 1000, 2),
                               'failure': 'transport'})
            raise VerificationError('HTTPS request failed; credentials and URL omitted') from None
        finally:
            connection.close()
        self.trace.append({'label': label, 'method': method, 'status': result['status'],
                           'path_sha256': hashlib.sha256(target.encode()).hexdigest(),
                           'duration_ms': round((time.monotonic() - started) * 1000, 2),
                           'body_bytes': len(body), 'body_sha256': hashlib.sha256(body).hexdigest(),
                           'headers': safe_headers(values)})
        return result


def pixels(data, expected_format):
    from PIL import Image
    with Image.open(BytesIO(data)) as image:
        require(image.format == expected_format and getattr(image, 'n_frames', 1) == 1,
                'Image format or frame count differs')
        with image.convert('RGBA') as rgba:
            return rgba.size, rgba.tobytes()


def image_response(response, item):
    require(response['status'] == 200, 'Signed image did not return HTTP 200')
    headers, body = response['headers'], response['body']
    require(headers.get('content-type') == item['mime'], 'Image MIME differs')
    require(headers.get('etag') == item['etag'], 'Image ETag differs')
    require(headers.get('x-content-type-options') == 'nosniff', 'Image nosniff header is missing')
    require(headers.get('content-length', str(len(body))) == str(len(body)), 'Image length differs')
    require(len(body) == item['bytes'] and hashlib.sha256(body).hexdigest() == item['sha256'],
            'Image bytes differ from the verified release')
    public_cache(headers)
    size, rgba = pixels(body, 'WEBP' if item['mime'] == 'image/webp' else 'PNG')
    require(size == (item['width'], item['height']), 'Image dimensions differ')
    return size, hashlib.sha256(rgba).hexdigest()


def samples(artifact, protocol, *, before):
    """Use the published HTTP corpus, not a newly rendered local baseline."""
    def report(name):
        return json.loads(read(artifact, 'results/' + name + '.json'))

    result = []

    def add(label, url, details, version, encoding, path_name):
        parsed = urlsplit(url)
        require(url == FIXTURE_ORIGIN + parsed.path and not parsed.query and not parsed.fragment,
                'Unexpected public fixture origin')
        records = protocol.parse_path(parsed.path, FIXTURE_KEYS)
        require(protocol.path_version(parsed.path) == version and protocol.path_encoding(parsed.path) == encoding,
                'Public fixture generation or encoding differs')
        expected = read(artifact, 'results/' + path_name)
        require(hashlib.sha256(expected).hexdigest() == details['sha256'], 'Reference image hash differs')
        result.append({'label': label, 'records': records, 'version': version, 'encoding': encoding,
                       'mime': 'image/webp' if encoding == 'w1' else 'image/png',
                       **{key: details[key] for key in ('sha256', 'bytes', 'width', 'height', 'etag')}})

    legacy = report('legacy')['records']
    require(len(legacy) == 5, 'Expected five legacy representatives')
    for row in legacy:
        add(row['version'] + '/' + row['case'], row['url'], row, row['version'], 'png', row['png'])
    if before:
        return result
    seen = set()
    for row in report('images')['posts']:
        if not row.get('url') or row['url'] in seen:
            continue
        seen.add(row['url'])
        add('v5/png/' + row['case'] + '/' + str(row['number']), row['url'], row['image'],
            'v5', 'png', row['image']['path'])
    webp = report('webp-http')['corpus']
    require(len(seen) == len(webp) == 45, 'Expected 45 distinct inputs for each v5 format')
    for row in webp:
        add('v5/w1/' + row['id'], row['url'], row, 'v5', 'w1', row['path'])
    require({x['records'] for x in result if x['version'] == 'v5' and x['encoding'] == 'png'}
            == {x['records'] for x in result if x['encoding'] == 'w1'}, 'PNG and WebP input sets differ')
    return result


def contract(client, path, item):
    opposite = 'image/png' if item['encoding'] == 'w1' else 'image/webp'
    head = client.request(path, label=item['label'] + '/head', method='HEAD', accept=opposite)
    require(head['status'] == 200 and not head['body'], 'HEAD status or body differs')
    headers = head['headers']
    require(headers.get('content-type') == item['mime'] and headers.get('etag') == item['etag']
            and headers.get('content-length', str(item['bytes'])) == str(item['bytes']), 'HEAD image metadata differs')
    public_cache(headers)
    conditional = client.request(path, label=item['label'] + '/conditional', etag=item['etag'], accept=opposite)
    require(conditional['status'] == 304 and not conditional['body']
            and conditional['headers'].get('etag') == item['etag'], 'Conditional response differs')
    if 'cache-control' in conditional['headers']:
        public_cache(conditional['headers'])
    repeated = client.request(path, label=item['label'] + '/repeat', accept=opposite)
    image_response(repeated, item)
    # This records what the CDN actually does. A request no-cache directive is
    # not proof of execution in the function; inspect the returned cache state.
    revalidated = client.request(path, label=item['label'] + '/revalidate', revalidate=True)
    image_response(revalidated, item)
    filename = path.rsplit('/', 1)[1].split('.')
    filename[1] = ('A' if filename[1][0] != 'A' else 'B') + filename[1][1:]
    bad = path.rsplit('/', 1)[0] + '/' + '.'.join(filename)
    rejected = [('signature', bad, {}, 403), ('query', path, {'query': 'quality=90'}, 400),
                ('method', path, {'method': 'POST'}, 405),
                ('generation', path.replace('/v5/', '/v4/', 1), {}, 403)]
    if item['encoding'] == 'w1':
        rejected.extend([('profile', path.replace('.w1.webp', '.w2.webp'), {}, 403),
                         ('extension', path.replace('.w1.webp', '.png'), {}, 403)])
    for name, target, options, status in rejected:
        response = client.request(target, label=item['label'] + '/reject-' + name, **options)
        require(response['status'] == status and 'no-store' in directives(response['headers'].get('cache-control', ''))
                and 'etag' not in response['headers'], 'Invalid request was accepted or cacheable: ' + name)
    return {'head': True, 'conditional_get': True, 'accept_does_not_negotiate': True,
            'rejected': [row[0] for row in rejected],
            'repeat_cache': repeated['headers'].get('x-vercel-cache'),
            'revalidation_cache': revalidated['headers'].get('x-vercel-cache')}


def check(bundle, artifact, base_url, allowed_hosts, output, *, phase, key_id=None, environment=None, oidc=None):
    environment = os.environ if environment is None else environment
    require(phase in ('before', 'candidate', 'production'), 'Unknown verification phase')
    host = origin(base_url, allowed_hosts)
    bundle, artifact, output = Path(bundle).absolute(), Path(artifact).absolute(), Path(output).absolute()
    require(not output.exists() and not output.is_symlink()
            and not any(p.is_symlink() for p in output.parents), 'Expected a new unlinked output file')
    require(not output.resolve().is_relative_to(bundle.resolve())
            and not output.resolve().is_relative_to(artifact.resolve()), 'Output must be outside verification inputs')
    manifest, evidence = verify_bundle(bundle), verify_public(artifact)
    require(manifest['manifest_sha256'] == evidence['candidate_manifest_sha256'], 'Public evidence belongs to another candidate')
    bypass = environment.get('VERCEL_AUTOMATION_BYPASS_SECRET') if phase == 'candidate' else None
    share = environment.get('VERCEL_SHARE_TOKEN') if phase == 'candidate' else None
    if phase != 'candidate':
        oidc = None
    elif oidc is None and environment.get('VERCEL_GITHUB_OIDC') == 'true':
        from scripts.vercel_api import GitHubOIDC
        oidc = GitHubOIDC(environment)
    client = HTTPS(host, bypass=bypass, oidc=oidc)
    result = {'schema_version': 1, 'phase': phase, 'host': host, 'state': 'running',
              'source_commit': evidence['source_commit'], 'candidate_manifest_sha256': manifest['manifest_sha256'],
              'checks': {}, 'images': [], 'requests': client.trace,
              'deployment_protection_bypass': bool(bypass or share or oidc),
              'deployment_protection_mode': 'oidc' if oidc else 'share' if share else 'automation' if bypass else 'none',
              'scope': 'HTTPS and CDN behavior; memory and cold-process execution require platform observations.'}
    try:
        require(sum(bool(value) for value in (bypass, share, oidc)) <= 1, 'Only one preview authentication method is allowed')
        with protocol_from_bundle(bundle) as protocol:
            selected, secret = signing_key(protocol, environment, key_id)
            corpus = samples(artifact, protocol, before=phase == 'before')
            if share:
                client.bootstrap_share(share)
            health = client.request('/healthz', label='health')
            require(health['status'] == 200 and health['body'] == b'ok\n'
                    and 'no-store' in directives(health['headers'].get('cache-control', '')), 'Health check failed')
            rgba_png = {}
            representatives = {}
            for item in corpus:
                print('Check ' + item['label'], flush=True)
                path = protocol.make_path(item['records'], selected, secret, version=item['version'], encoding=item['encoding'])
                response = client.request(path, label=item['label'], accept='image/png' if item['encoding'] == 'w1' else 'image/webp')
                rgba = image_response(response, item)
                if item['version'] == 'v5':
                    if item['encoding'] == 'png':
                        rgba_png[item['records']] = rgba
                    else:
                        require(rgba == rgba_png[item['records']], 'WebP changed PNG pixels')
                    if 'nankai-advisory' in item['label']:
                        representatives[item['encoding']] = (path, item)
                result['images'].append({key: item[key] for key in ('label', 'version', 'encoding', 'sha256', 'bytes')})
            if phase != 'before':
                require(set(representatives) == {'png', 'w1'}, 'Missing HTTP contract representatives')
                result['checks']['contracts'] = {encoding: contract(client, path, item)
                                                 for encoding, (path, item) in representatives.items()}
                result['checks']['rgba_pixels_exact'] = True
                hits = sum(row.get('headers', {}).get('x-vercel-cache') == 'HIT' for row in client.trace)
                result['checks']['cdn_hit_count'] = hits
                if phase == 'production':
                    require(all(value['repeat_cache'] == 'HIT' for value in result['checks']['contracts'].values()),
                            'Production CDN cache hits were not observed for both v5 formats')
            require(verify_bundle(bundle) == manifest, 'Candidate changed during HTTPS verification')
            result['checks'].update(health=True, image_bytes_exact=True, immutable_candidate=True)
            result['state'] = 'success'
    except Exception as error:
        result['state'] = 'failed'
        # Response bodies, URLs, signing configuration and raw exceptions are
        # deliberately excluded. Request labels/statuses locate failed checks.
        result['failure'] = str(error) if isinstance(error, VerificationError) else type(error).__name__
        raise
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--public-artifact', required=True, type=Path)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--allowed-host', required=True, action='append')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--phase', choices=('before', 'candidate', 'production'), required=True)
    parser.add_argument('--key-id')
    args = parser.parse_args(argv)
    try:
        result = check(args.bundle, args.public_artifact, args.base_url, args.allowed_host,
                       args.output, phase=args.phase, key_id=args.key_id)
    except Exception:
        print('Vercel verification failed; inspect the status-only report. Credentials and URLs are omitted.', file=sys.stderr)
        return 1
    print(json.dumps({'state': result['state'], 'phase': result['phase'], 'images': len(result['images']),
                      'requests': len(result['requests']), 'output': str(args.output.absolute())}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
