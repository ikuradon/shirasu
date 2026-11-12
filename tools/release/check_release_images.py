#!/usr/bin/env python3
"""Exercise exported posts against isolated image artifacts, without a network.

Run in the image dependency environment with the artifact first on PYTHONPATH.
The receiver export contains public fixtures and ephemeral signed events only.
All request handling and rendering use the artifact's actual WSGI application.
"""

import argparse
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import struct
import sys
from urllib.parse import urlsplit
import zlib


KEY = bytes(range(32))  # Public rehearsal key, never a deployment credential.
KEY_ID = 'rehearsal'
ORIGIN = 'https://images.example.test'
LEGACY_CASES = (('v1', 'typhoon-track'), ('v2', 'marine'), ('v3', 'weather'),
                ('v4', 'weather'), ('v4', 'nankai-advisory'))
_HEX = re.compile(r'[0-9a-f]{64}')
_SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')


def _require(value, message):
    if not value:
        raise ValueError(message)


def _read_json(path):
    path = Path(path)
    _require(not path.is_symlink() and path.is_file(), 'Expected a regular JSON input file')
    with path.open('rb') as source:
        data = source.read(16 * 1024 * 1024 + 1)
    _require(len(data) <= 16 * 1024 * 1024, 'JSON input exceeds the rehearsal limit')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, 'JSON input contains duplicate keys')
            value[key] = item
        return value
    value = json.loads(data, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
    _require(isinstance(value, dict), 'JSON input must be an object')
    return value, hashlib.sha256(data).hexdigest()


def _origin(module, directory):
    path = getattr(module, '__file__', None)
    _require(path is not None and Path(path).resolve().is_relative_to(directory.resolve()),
             'Runtime module was imported outside the isolated artifact: ' + module.__name__)


def _runtime(directory, *, candidate=False):
    directory = Path(directory).resolve()
    packaged = (directory / 'shirasu/images/protocol.py').is_file()
    protocol = importlib.import_module('shirasu.images.protocol' if packaged else 'image_protocol')
    http = importlib.import_module('image_service.app')
    for module in (protocol, http):
        _origin(module, directory)
    expected = ('v1', 'v2', 'v3', 'v4') + (('v5',) if candidate else ())
    _require(tuple(protocol.SUPPORTED_VERSIONS) == expected, 'Artifact URL generations differ from the rehearsal')
    _require(tuple(http.RENDERERS) == expected, 'Artifact HTTP generations differ from its protocol')
    if candidate:
        profile = importlib.import_module('shirasu.images.versions' if packaged else 'image_versions')
        _origin(profile, directory)
        _require(profile.CANDIDATE_VERSION == 'v5' and profile.NANKAI_TOPIC_VERSIONS == ('v5',),
                 'Candidate topic capability is not enabled for v5 only')
    return protocol, http.app


def _runtime_origins(directory):
    for name, module in tuple(sys.modules.items()):
        if (name == 'image_service' or name.startswith('image_service.')
                or name in ('image_protocol', 'image_versions', 'dcx_instructions', 'nankai_image',
                            'shirasu.images.protocol', 'shirasu.images.versions',
                            'shirasu.images.nankai', 'shirasu.reports.dcx')):
            _origin(module, directory)


@contextmanager
def _test_keys(value=None):
    previous = os.environ.get('SHIRASU_IMAGE_KEYS')
    os.environ['SHIRASU_IMAGE_KEYS'] = json.dumps({KEY_ID: KEY.hex()}) if value is None else value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('SHIRASU_IMAGE_KEYS', None)
        else:
            os.environ['SHIRASU_IMAGE_KEYS'] = previous


def _request(app, path, *, method='GET', query='', etag=None):
    result = {}
    def start(status, headers):
        _require(not result, 'WSGI application called start_response more than once')
        _require(len({name.lower() for name, _ in headers}) == len(headers), 'Duplicate HTTP headers')
        result.update(status=status, headers={name.lower(): value for name, value in headers})
    environ = {'REQUEST_METHOD': method, 'PATH_INFO': path, 'QUERY_STRING': query}
    if etag is not None:
        environ['HTTP_IF_NONE_MATCH'] = etag
    stream = app(environ, start)
    try:
        result['body'] = b''.join(stream)
    finally:
        if hasattr(stream, 'close'):
            stream.close()
    _require('status' in result, 'WSGI application returned no response')
    return result


def _png_size(data):
    _require(data.startswith(b'\x89PNG\r\n\x1a\n'), 'HTTP response is not a PNG')
    offset, dimensions, image_data = 8, None, False
    while offset < len(data):
        _require(offset + 12 <= len(data), 'PNG chunk is truncated')
        size, = struct.unpack('>I', data[offset:offset + 4])
        end = offset + size + 12
        _require(end <= len(data), 'PNG payload is truncated')
        kind, payload = data[offset + 4:offset + 8], data[offset + 8:end - 4]
        checksum, = struct.unpack('>I', data[end - 4:end])
        _require(zlib.crc32(kind + payload) & 0xffffffff == checksum, 'PNG chunk checksum differs')
        if dimensions is None:
            _require(kind == b'IHDR' and size == 13, 'PNG has no valid initial header')
            dimensions = struct.unpack('>II', payload[:8])
        else:
            _require(kind != b'IHDR', 'PNG contains repeated headers')
        image_data |= kind == b'IDAT'
        if kind == b'IEND':
            _require(size == 0 and end == len(data) and image_data, 'PNG has an invalid ending')
            return dimensions
        offset = end
    raise ValueError('PNG has no ending')


def _png_response(response, dimensions):
    _require(response['status'] == '200 OK', 'A signed image did not return HTTP 200')
    data, headers = response['body'], response['headers']
    _require(_png_size(data) == dimensions, 'Image dimensions differ from its generation')
    digest = hashlib.sha256(data).hexdigest()
    expected = {'content-type': 'image/png', 'content-length': str(len(data)),
                'x-content-type-options': 'nosniff', 'etag': '"' + digest + '"',
                'cache-control': 'public, max-age=31536000, immutable',
                'vercel-cdn-cache-control': 'public, max-age=31536000'}
    _require(all(headers.get(name) == value for name, value in expected.items()), 'PNG HTTP headers differ')
    return {'sha256': digest, 'bytes': len(data), 'width': dimensions[0], 'height': dimensions[1],
            'etag': headers['etag']}


def _event(value):
    _require(isinstance(value, dict) and type(value.get('kind')) is int and value['kind'] == 1
             and isinstance(value.get('content'), str) and isinstance(value.get('tags'), list)
             and type(value.get('created_at')) is int and value['created_at'] >= 0,
             'Exported event has invalid metadata')
    for field, length in (('id', 64), ('pubkey', 64), ('sig', 128)):
        _require(isinstance(value.get(field), str) and re.fullmatch(f'[0-9a-f]{{{length}}}', value[field]),
                 'Exported event has invalid public signing fields')
    serial = [0, value['pubkey'], value['created_at'], 1, value['tags'], value['content']]
    digest = hashlib.sha256(json.dumps(serial, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    _require(digest == value['id'], 'Exported event content does not match its signed ID')
    return value


def _entries(value):
    _require(isinstance(value, list) and len(value) <= 1000, 'Invalid exported report list')
    result = []
    for entry in value:
        _require(isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str)
                 and type(entry[1]) is int, 'Invalid exported report entry')
        result.append(tuple(entry))
    return tuple(result)


def _indexes(value, count, *, allow_empty=False):
    _require(isinstance(value, list) and (allow_empty or value)
             and all(type(index) is int and 0 <= index < count for index in value)
             and len(set(value)) == len(value), 'Invalid exported report indexes')
    return tuple(value)


def _attachment(row, protocol, *, entries, text=None, version=None):
    event = _event(row.get('event'))
    url, alt = row.get('url'), row.get('alt')
    if url is None:
        _require(not entries and alt is None and not event['tags'], 'Text-only event has attachment metadata')
        if text is not None:
            _require(event['content'] == text, 'Text-only event differs from its planned text')
        return None
    _require(isinstance(url, str) and isinstance(alt, str) and alt.strip() and entries,
             'Image event has incomplete attachment metadata')
    parsed = urlsplit(url)
    _require(parsed.scheme == 'https' and parsed.netloc == 'images.example.test'
             and not parsed.query and not parsed.fragment and url == ORIGIN + parsed.path,
             'Exported image URL is not a rehearsal URL')
    actual_version = protocol.path_version(parsed.path)
    _require(parsed.path.split('/')[2] == KEY_ID, 'Exported image URL has a different test key ID')
    _require(version is None or actual_version == version, 'Exported image URL uses the wrong generation')
    restored = protocol.parse_path(parsed.path, {KEY_ID: KEY})
    _require(restored == tuple(sorted(entries, key=lambda entry: (entry[1], entry[0]))),
             'Signed image URL does not contain the original image inputs')
    _require(protocol.make_path(entries, KEY_ID, KEY, version=actual_version) == parsed.path,
             'Exported image URL is not canonical')
    _require(event['tags'] == [['imeta', 'url ' + url, 'm image/png', 'alt ' + alt]],
             'Image URL or ALT differs from the signed imeta tag')
    if text is not None:
        before, separator, after = text.partition('\n---\n')
        expected = before + '\n\n' + url + (separator + after if separator else '')
        _require(event['content'] == expected, 'Image URL is not at the actual posting position')
    else:
        _require(event['content'].count('\n\n' + url + '\n---\n') == 1,
                 'Transition event does not include its image URL exactly once')
    return parsed.path


def _export(path, *, manifest=None):
    value, digest = _read_json(path)
    _require(type(value.get('schema_version')) is int and value['schema_version'] == 1
             and value.get('candidate_version') == 'v5' and value.get('rollback_version') == 'v4'
             and isinstance(value.get('posts'), list) and value['posts']
             and isinstance(value.get('transition_posts'), list)
             and isinstance(value.get('bundle_manifest_sha256'), str)
             and _HEX.fullmatch(value['bundle_manifest_sha256']), 'Invalid receiver rehearsal export')
    if manifest is not None:
        _require(value['bundle_manifest_sha256'] == manifest['manifest_sha256'],
                 'Receiver export belongs to another candidate artifact')
    cases = {}
    for row in value['posts']:
        _require(isinstance(row, dict) and isinstance(row.get('case'), str) and _SLUG.fullmatch(row['case'])
                 and row.get('kind') in ('single', 'typhoon', 'nankai') and isinstance(row.get('text'), str),
                 'Invalid exported post')
        entries = _entries(row.get('records'))
        indexes = _indexes(row.get('record_indexes'), len(entries))
        image_indexes = _indexes(row.get('image_record_indexes'), len(entries), allow_empty=True)
        _require(set(image_indexes) <= set(indexes), 'Image inputs are outside the post input set')
        previous = cases.setdefault(row['case'], {'records': entries, 'count': 0})
        previous['count'] += 1
        _require(previous['records'] == entries and type(row.get('number')) is int
                 and row['number'] == previous['count'], 'Exported case inputs or post order differ')
        _, separator, nmea = row['text'].partition('\n---\n')
        _require(separator and nmea.splitlines() == ['NMEA: ' + entries[index][0] for index in indexes],
                 'Post text does not preserve every original NMEA in order')
    _require(type(value.get('case_count')) is int and value['case_count'] == len(cases)
             and type(value.get('post_count')) is int and value['post_count'] == len(value['posts']),
             'Exported case or post counts differ')
    return value, digest


def _fresh_output(output):
    output = Path(output).absolute()
    _require(not output.exists() and not output.is_symlink(), 'Rehearsal output already exists')
    images = output.parent / (output.stem + '-images')
    _require(not images.exists() and not images.is_symlink(), 'Rehearsal PNG directory already exists')
    return output, images


def _write_output(output, value):
    with output.open('x', encoding='utf-8') as target:
        json.dump(value, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.write('\n')


def record_legacy(package, events, output):
    """Record actual v1-v4 responses in a separate process using the old artifact."""
    output, images = _fresh_output(output)
    package = Path(package).resolve()
    _require(not output.is_relative_to(package) and not images.is_relative_to(package),
             'Rehearsal output must be outside the old image artifact')
    exported, events_digest = _export(events)
    protocol, app = _runtime(package)
    samples = {row['case']: row for row in reversed(exported['posts'])}
    _require(all(case in samples for _, case in LEGACY_CASES), 'Legacy comparison samples are missing')
    output.parent.mkdir(parents=True, exist_ok=True)
    images.mkdir()
    records = []
    with _test_keys():
        for version, case in LEGACY_CASES:
            entries = _entries(samples[case]['records'])
            path = protocol.make_path(entries, KEY_ID, KEY, version=version)
            print(f'legacy {version} / {case}', flush=True)
            response = _request(app, path)
            item = _png_response(response, (1440, 1200))
            filename = f'{version}-{case}.png'
            (images / filename).write_bytes(response['body'])
            records.append({'version': version, 'case': case, 'entries': entries,
                            'url': ORIGIN + path, 'png': images.name + '/' + filename, **item})
        _runtime_origins(package)
    result = {'schema_version': 1, 'mode': 'legacy', 'receiver_export_sha256': events_digest,
              'records': records, 'checks': {'isolated_runtime': True, 'real_wsgi_pngs': True}}
    _write_output(output, result)
    return result


def _compare_legacy(path, exported_digest, protocol, app):
    baseline, digest = _read_json(path)
    _require(baseline.get('schema_version') == 1 and baseline.get('mode') == 'legacy'
             and baseline.get('receiver_export_sha256') == exported_digest
             and isinstance(baseline.get('records'), list)
             and all(isinstance(row, dict) for row in baseline['records'])
             and [(row.get('version'), row.get('case')) for row in baseline['records']] == list(LEGACY_CASES),
             'Invalid legacy comparison or mismatched receiver export')
    checked = []
    for row in baseline['records']:
        entries = _entries(row.get('entries'))
        actual = protocol.make_path(entries, KEY_ID, KEY, version=row['version'])
        _require(row.get('url') == ORIGIN + actual, 'Old signed URL changed in the candidate')
        print(f'preserved {row["version"]} / {row["case"]}', flush=True)
        image = _png_response(_request(app, actual), (1440, 1200))
        _require(all(image[key] == row.get(key) for key in ('sha256', 'bytes', 'width', 'height', 'etag')),
                 'Old PNG or HTTP validator changed in the candidate')
        checked.append({'version': row['version'], 'case': row['case'], **image})
    return {'baseline_sha256': digest, 'records': checked}


def _errors(app, protocol, posts, good_path):
    # Alter a signature character, not its padding bits, to retain canonical encoding.
    prefix, filename = good_path.rsplit('/', 1)
    payload, signature, extension = filename.split('.')
    bad = prefix + '/' + payload + '.' + ('A' if signature[0] != 'A' else 'B') + signature[1:] + '.' + extension
    weather = next(row for row in posts if row['case'] == 'weather')['records'][0]
    marine = next(row for row in posts if row['case'] == 'marine')['records'][0]
    mixed = ((weather[0], weather[1]), (marine[0], weather[1]))
    invalid_bundle = protocol.make_path(mixed, KEY_ID, KEY, version='v5')
    checks = [('bad_signature', bad, {}, '403 Forbidden'),
              ('changed_generation', good_path.replace('/v5/', '/v4/', 1), {}, '403 Forbidden'),
              ('unknown_generation', good_path.replace('/v5/', '/v99/', 1), {}, '404 Not Found'),
              ('query', good_path, {'query': 'color=red'}, '400 Bad Request'),
              ('method', good_path, {'method': 'POST'}, '405 Method Not Allowed'),
              ('invalid_bundle', invalid_bundle, {}, '400 Bad Request'),
              ('path_limit', '/v5/' + 'a' * protocol.MAX_PATH_LENGTH, {}, '414 URI Too Long')]
    result = {}
    for name, path, options, expected in checks:
        response = _request(app, path, **options)
        _require(response['status'] == expected and response['headers'].get('cache-control') == 'no-store',
                 'Rejected URL has incorrect status or cache policy: ' + name)
        _require('etag' not in response['headers'] and KEY.hex().encode() not in response['body']
                 and b'$QZQSM' not in response['body'], 'Error response reflects sensitive input')
        result[name] = expected
    with _test_keys(''):
        response = _request(app, good_path)
    _require(response['status'] == '503 Service Unavailable'
             and response['headers'].get('cache-control') == 'no-store', 'Unconfigured service must fail closed')
    result['unconfigured'] = response['status']
    return result


def check_candidate(bundle, events, legacy_result, output):
    """Verify real signed post URLs against only the candidate's image sources."""
    from tools.release.prepare_release_candidate import verify_bundle

    output, images = _fresh_output(output)
    bundle = Path(bundle).resolve()
    _require(not output.is_relative_to(bundle) and not images.is_relative_to(bundle),
             'Rehearsal output must be outside the immutable candidate artifact')
    manifest = verify_bundle(bundle)
    exported, events_digest = _export(events, manifest=manifest)
    protocol, app = _runtime(bundle / 'images', candidate=True)
    attachments = []
    for row in exported['posts']:
        entries = _entries(row['records'])
        image_entries = tuple(entries[index] for index in row['image_record_indexes'])
        path = _attachment(row, protocol, entries=image_entries, text=row['text'], version='v5')
        attachments.append((row, path))
    transitions = []
    for row in exported['transition_posts']:
        _require(isinstance(row, dict) and isinstance(row.get('label'), str), 'Invalid transition post')
        entries = _entries(row.get('entries'))
        path = _attachment(row, protocol, entries=entries)
        transitions.append((row, path))
    _require(any(path for _, path in attachments), 'Candidate export has no image attachments')
    # Validate all event/input associations before creating partial output.
    output.parent.mkdir(parents=True, exist_ok=True)
    images.mkdir()
    rendered = {}
    results = []
    with _test_keys():
        health = _request(app, '/healthz')
        _require(health['status'] == '200 OK' and health['body'] == b'ok\n'
                 and health['headers'].get('cache-control') == 'no-store', 'Candidate asset health check failed')
        legacy = _compare_legacy(legacy_result, events_digest, protocol, app)
        for row, path in attachments:
            if path is None:
                results.append({'case': row['case'], 'number': row['number'], 'image': None})
                continue
            print(f'post {row["case"]} / {row["number"]}', flush=True)
            response = _request(app, path)
            image = _png_response(response, (1920, 1080))
            name = f'{row["case"]}-{row["number"]:02d}.png'
            (images / name).write_bytes(response['body'])
            rendered[path] = image
            results.append({'case': row['case'], 'number': row['number'], 'url': ORIGIN + path,
                            'event_id': row['event']['id'], 'alt': row['alt'],
                            'image': {'path': images.name + '/' + name, **image}})
        transition_results = []
        for number, (row, path) in enumerate(transitions, 1):
            if path is None:
                transition_results.append({'label': row['label'], 'image': None})
                continue
            if path not in rendered:
                version = protocol.path_version(path)
                dimensions = (1920, 1080) if version == 'v5' else (1440, 1200)
                response = _request(app, path)
                image = _png_response(response, dimensions)
                name = f'transition-{number:02d}-{version}.png'
                (images / name).write_bytes(response['body'])
                rendered[path] = image
            transition_results.append({'label': row['label'], 'url': ORIGIN + path,
                                       'event_id': row['event']['id'], **rendered[path]})
        # Use a small topic image for the extra actual rendering required by HEAD and 304.
        representative = next((path for row, path in attachments if row['case'] == 'nankai-advisory'),
                              next(path for _, path in attachments if path))
        original = rendered[representative]
        head = _request(app, representative, method='HEAD')
        _require(head['status'] == '200 OK' and not head['body']
                 and head['headers'].get('content-length') == str(original['bytes'])
                 and head['headers'].get('etag') == original['etag']
                 and head['headers'].get('content-type') == 'image/png'
                 and head['headers'].get('x-content-type-options') == 'nosniff'
                 and head['headers'].get('cache-control') == 'public, max-age=31536000, immutable'
                 and head['headers'].get('vercel-cdn-cache-control') == 'public, max-age=31536000',
                 'HEAD differs from GET')
        conditional = _request(app, representative, etag=original['etag'])
        _require(conditional['status'] == '304 Not Modified' and not conditional['body']
                 and conditional['headers'].get('etag') == original['etag']
                 and conditional['headers'].get('cache-control') == 'public, max-age=31536000, immutable',
                 'Conditional GET differs from GET')
        errors = _errors(app, protocol, exported['posts'], representative)
        _runtime_origins(bundle / 'images')
    _require(verify_bundle(bundle)['manifest_sha256'] == manifest['manifest_sha256'],
             'Candidate artifact changed during rendering')
    result = {'schema_version': 1, 'mode': 'candidate', 'candidate_version': 'v5',
              'bundle_manifest_sha256': manifest['manifest_sha256'], 'receiver_export_sha256': events_digest,
              'case_count': exported['case_count'], 'post_count': exported['post_count'],
              'attachment_count': sum(path is not None for _, path in attachments),
              'posts': results, 'transition_posts': transition_results, 'legacy': legacy,
              'checks': {'isolated_runtime': True, 'health': True, 'real_wsgi_pngs': True,
                         'event_ids': True, 'signed_inputs': True, 'imeta_and_body': True,
                         'head': True, 'conditional_get': True, 'artifact_unchanged': True,
                         'rejected_requests': errors},
              'scope': 'Local WSGI and real rendering; no CDN, TLS, deployment or relay traffic.'}
    _write_output(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--bundle', type=Path)
    mode.add_argument('--legacy-package', type=Path)
    parser.add_argument('--events', required=True, type=Path)
    parser.add_argument('--legacy-result', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if bool(args.bundle) != bool(args.legacy_result):
        parser.error('--bundle requires --legacy-result; legacy mode does not accept it')
    try:
        result = (check_candidate(args.bundle, args.events, args.legacy_result, args.output) if args.bundle
                  else record_legacy(args.legacy_package, args.events, args.output))
    except (OSError, ValueError, KeyError, ImportError) as error:
        print(f'Image release rehearsal failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps({'mode': result['mode'], 'output': str(args.output.absolute()),
                      'images': result.get('attachment_count', len(result.get('records', ())))}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
