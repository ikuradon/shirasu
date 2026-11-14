#!/usr/bin/env python3
"""Verify real receiver events and loopback HTTP WebP from one fixed artifact.

Run receiver/images phases in their respective dependency environments with
the corresponding candidate directory first on PYTHONPATH. Keys below are
public test fixtures; no configuration, existing database or relay is used.
Every actual post is mandatory. Image-only examples with extra cancellation
context or a different pre-canonicalization order are reported separately;
this is a comparison-scope distinction, not a claim that unordered bundles
cannot be encoded as canonical signed URLs.
"""

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import http.client
from io import BytesIO
import json
from pathlib import Path
import threading
from unittest.mock import patch
from urllib.parse import urlsplit
from wsgiref.simple_server import make_server, WSGIRequestHandler

from tools.release import check_release_images as images
from tools.release import check_release_receiver as receiver

KEY, KEY_ID, ORIGIN = receiver.KEY, receiver.KEY_ID, receiver.ORIGIN
require = receiver.require


def event_details(modules, event, text, entries, publisher):
    """Check the actual SDK event, without replacing its URL, MIME or content."""
    require(event.verify() and event.kind().as_u16() == 1, 'invalid signed text event')
    tags = [tag.to_vec() for tag in event.tags()]
    url, alt = None, None
    if entries:
        require(publisher is not None, 'image requires a publisher')
        url = publisher.url(entries)
        alt = modules['image_alt'].image_alt_text(entries)
        require(tags == images.posting_tags(event.content(), url=url, mime=publisher.mime, alt=alt),
                'real Nostr MIME, URL, ALT or hashtags differ')
        path = urlsplit(url).path
        actual = modules['image_protocol'].parse_path(path, {KEY_ID: KEY})
        require(actual == tuple(sorted(entries, key=lambda entry: (entry[1], entry[0]))),
                'signed URL lost original image inputs')
        expected = modules['image_publication'].with_image(text, url)
    else:
        require(tags == images.posting_tags(event.content()), 'text-only event has unexpected metadata')
        expected = text
    require(event.content() == expected, 'actual signed content changed')
    return {'event': json.loads(event.as_json()), 'url': url, 'alt': alt}


def check_receiver(bundle, output):
    from tools.release.prepare_release_candidate import verify_bundle

    bundle, output = Path(bundle).resolve(), Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'output already exists')
    require(not output.resolve().is_relative_to(bundle), 'output must be outside the fixed bundle')
    manifest = verify_bundle(bundle)
    modules = receiver.runtime_modules(bundle)
    require(modules['image_versions'].WEBP_VERSIONS == ('v5',), 'candidate WebP capability differs')
    base_transport = receiver.LocalTransport

    class Transport(base_transport):
        def build_event(self, message, image_url=None, image_alt=None, image_mime='image/png'):
            return self.main.Nostr.build_event(self, message, image_url=image_url,
                                               image_alt=image_alt, image_mime=image_mime)

    def publisher(runtime, version):
        if version is None:
            return None
        return runtime['image_publication'].ImagePublisher.from_environment({
            'SHIRASU_IMAGE_BASE_URL': ORIGIN, 'SHIRASU_IMAGE_KEY_ID': KEY_ID,
            'SHIRASU_IMAGE_SIGNING_KEY': KEY.hex(), 'SHIRASU_IMAGE_VERSION': version,
            'SHIRASU_IMAGE_ENCODING': 'w1' if version == 'v5' else 'png'})

    with receiver.isolated_runtime(modules), \
            patch.object(receiver, 'LocalTransport', Transport), \
            patch.object(receiver, 'publisher', publisher), \
            patch.object(receiver, 'event_details', event_details):
        result = asyncio.run(receiver.run_checks(modules, manifest))
    require(verify_bundle(bundle) == manifest, 'candidate changed during receiver verification')
    result.update(mode='webp-receiver', encoding='w1')
    result['checks']['real_nostr_mime'] = True
    output.parent.mkdir(parents=True, exist_ok=True)
    images._write_output(output, result)
    return result


def attachment(row, protocol, *, entries, text=None, version=None, encoding=None):
    event = images._event(row.get('event'))
    url, alt = row.get('url'), row.get('alt')
    if url is None:
        require(not entries and alt is None and event['tags'] == images.posting_tags(event['content']),
                'text-only attachment or hashtags changed')
        if text is not None:
            require(event['content'] == text, 'text-only body differs')
        return None
    require(isinstance(url, str) and isinstance(alt, str) and alt.strip() and entries,
            'attachment metadata is incomplete')
    parsed = urlsplit(url)
    require(parsed.scheme == 'https' and parsed.netloc == 'images.example.test'
            and not parsed.query and not parsed.fragment and url == ORIGIN + parsed.path,
            'unexpected attachment origin')
    actual_version, actual_encoding = protocol.path_version(parsed.path), protocol.path_encoding(parsed.path)
    require(version is None or actual_version == version, 'attachment generation differs')
    require(encoding is None or actual_encoding == encoding, 'attachment encoding differs')
    require(protocol.parse_path(parsed.path, {KEY_ID: KEY}) ==
            tuple(sorted(entries, key=lambda entry: (entry[1], entry[0]))), 'signed inputs differ')
    require(protocol.make_path(entries, KEY_ID, KEY, version=actual_version,
                               encoding=actual_encoding) == parsed.path, 'attachment URL is not canonical')
    mime = 'image/webp' if actual_encoding == 'w1' else 'image/png'
    require(event['tags'] == images.posting_tags(event['content'], url=url, mime=mime, alt=alt),
            'imeta URL, MIME, ALT or hashtags differ')
    require(images.posting_content_matches(event['content'], url, text),
            'URL position or original text differs')
    return parsed.path


@contextmanager
def local_http(app):
    """Run the real WSGI app over TCP loopback; never resolve an external host."""
    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *args):
            pass

    server = make_server('127.0.0.1', 0, app, handler_class=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()

    def request(path, *, method='GET', query='', etag=None, accept=None):
        require(isinstance(path, str) and path.startswith('/') and not path.startswith('//'),
                'loopback request requires an absolute path')
        headers = {}
        if etag is not None:
            headers['If-None-Match'] = etag
        if accept is not None:
            headers['Accept'] = accept
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=180)
        try:
            connection.request(method, path + ('?' + query if query else ''), headers=headers)
            response = connection.getresponse()
            pairs = response.getheaders()
            require(len({name.lower() for name, _ in pairs}) == len(pairs), 'duplicate HTTP headers')
            return {'status': f'{response.status} {response.reason}',
                    'headers': {name.lower(): value for name, value in pairs}, 'body': response.read()}
        finally:
            connection.close()
    try:
        yield request
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def rgba(data, expected_format):
    from PIL import Image
    with Image.open(BytesIO(data)) as source:
        require(source.format == expected_format and getattr(source, 'n_frames', 1) == 1,
                'response uses the wrong format or animation')
        source.load()
        with source.convert('RGBA') as pixels:
            return pixels.size, pixels.tobytes()


def webp_response(response, reference_png):
    require(response['status'] == '200 OK', 'signed WebP did not return HTTP 200')
    data, headers = response['body'], response['headers']
    require(data[:4] == b'RIFF' and data[8:12] == b'WEBP', 'response is not WebP')
    size, pixels = rgba(data, 'WEBP')
    expected_size, expected_pixels = rgba(reference_png, 'PNG')
    require((size, pixels) == (expected_size, expected_pixels), 'WebP changed the approved RGBA pixels')
    digest = hashlib.sha256(data).hexdigest()
    expected = {'content-type': 'image/webp', 'content-length': str(len(data)),
                'x-content-type-options': 'nosniff', 'etag': '"' + digest + '"',
                'cache-control': 'public, max-age=31536000, immutable',
                'vercel-cdn-cache-control': 'public, max-age=31536000'}
    require(all(headers.get(name) == value for name, value in expected.items()), 'WebP HTTP contract differs')
    return {'sha256': digest, 'bytes': len(data), 'width': size[0], 'height': size[1],
            'etag': expected['etag'], 'mime': 'image/webp', 'pixel_exact': True}


def gallery_inputs(baseline):
    """Deduplicate case/post inputs while keeping explicit cancellation refs separate."""
    source_inputs = dict(baseline._inputs)
    files = {name: data for name, _, data in baseline._assets}
    rows, seen = [], {}
    for case in baseline.manifest['cases']:
        _, records, reference = source_inputs[case['slug']]
        indexes = case.get('preview_record_indexes', range(len(records)))
        bundles = [('case', None, tuple(records[index] for index in indexes), reference, case['images']['preview'])]
        bundles.extend(('post', number, tuple(records[index] for index in post.get(
            'image_record_indexes', post['record_indexes'])), (), post['images']['preview'])
                       for number, post in enumerate(case['posts'], 1) if post['image_enabled'])
        for role, number, records, reference, image in bundles:
            key = records, reference
            usage = {'case': case['slug'], 'role': role, 'number': number}
            if key in seen:
                require(seen[key]['png_sha256'] == image['sha256'], 'identical inputs have different approved PNGs')
                seen[key]['usages'].append(usage)
                continue
            row = {'id': case['slug'] if role == 'case' else f'{case["slug"]}-post-{number:02d}',
                   'records': records, 'cancel_reference': reference,
                   'png': files[image['path']], 'png_sha256': image['sha256'], 'usages': [usage]}
            seen[key] = row
            rows.append(row)
    return rows


def compare_posts(exported, baseline, protocol):
    source_inputs = dict(baseline._inputs)
    approved = {(case['slug'], number): post for case in baseline.manifest['cases']
                for number, post in enumerate(case['posts'], 1)}
    actual = {(post['case'], post['number']): post for post in exported['posts']}
    require(len(actual) == len(exported['posts']) and set(actual) == set(approved), 'post set differs from approved gallery')
    result = []
    for key, expected in approved.items():
        row = actual[key]
        _, records, _ = source_inputs[key[0]]
        for name, value in (('records', records), ('text', expected['text']), ('kind', expected['kind']),
                            ('record_indexes', expected['record_indexes']),
                            ('image_record_indexes', expected['image_record_indexes'])):
            require(json.dumps(row[name], sort_keys=True) == json.dumps(value, sort_keys=True),
                    f'{key}: {name} differs from approved posting inputs')
        entries = tuple(records[index] for index in row['image_record_indexes'])
        path = attachment(row, protocol, entries=entries, text=expected['text'], version='v5', encoding='w1')
        if path:
            require(row['alt'] == expected['images']['preview']['alt'], 'approved ALT changed')
        result.append((row, path))
    return result


def offline_reason(row, canonical_records):
    """Exclude only image-only context that the signed posting URL cannot retain.

    Actual posts always remain mandatory, even if their original record order
    changes during canonicalization. Their HTTP pixels must still match the
    approved post; this must never turn a posting mismatch into a skipped case.
    """
    is_post = any(usage['role'] == 'post' for usage in row['usages'])
    if row['cancel_reference']:
        require(not is_post, 'actual post unexpectedly depends on an offline cancellation reference')
        return {'reason': 'explicit cancellation reference is offline-only'}
    if not is_post and row['records'] != canonical_records:
        return {'reason': 'image-only original record order is not retained by signed URL canonicalization',
                'canonical_record_indexes': [row['records'].index(entry) for entry in canonical_records]}
    return None


def request_contract(request, path, original):
    head = request(path, method='HEAD', accept='image/png')
    require(head['status'] == '200 OK' and not head['body'], 'WebP HEAD status/body differs')
    expected = {'content-type': 'image/webp', 'content-length': str(original['bytes']),
                'etag': original['etag'], 'x-content-type-options': 'nosniff',
                'cache-control': 'public, max-age=31536000, immutable',
                'vercel-cdn-cache-control': 'public, max-age=31536000'}
    require(all(head['headers'].get(key) == value for key, value in expected.items()), 'WebP HEAD headers differ')
    conditional = request(path, etag=original['etag'], accept='image/png')
    require(conditional['status'] == '304 Not Modified' and not conditional['body'], 'WebP conditional GET differs')
    require(all(conditional['headers'].get(key) == value for key, value in expected.items()
                if key != 'content-length'), 'WebP 304 headers differ')
    prefix, filename = path.rsplit('/', 1)
    payload, signature, profile, extension = filename.split('.')
    changed = ('A' if signature[0] != 'A' else 'B') + signature[1:]
    rejected = [
        ('signature', f'{prefix}/{payload}.{changed}.{profile}.{extension}', {}, '403 Forbidden'),
        ('profile', path.replace('.w1.webp', '.w2.webp'), {}, '403 Forbidden'),
        ('extension', path.replace('.w1.webp', '.png'), {}, '403 Forbidden'),
        ('generation', path.replace('/v5/', '/v4/'), {}, '403 Forbidden'),
        ('query', path, {'query': 'quality=90'}, '400 Bad Request'),
        ('method', path, {'method': 'POST'}, '405 Method Not Allowed')]
    checks = {}
    for label, target, options, status in rejected:
        response = request(target, **options)
        require(response['status'] == status and response['headers'].get('cache-control') == 'no-store'
                and 'etag' not in response['headers'], 'invalid request accepted or cached: ' + label)
        checks[label] = status
    return {'head': True, 'conditional_get': True, 'accept_does_not_negotiate': True, 'rejected': checks}


def check_images(bundle, events, gallery, output):
    from tools.preview.gallery_compare import load_baseline
    from tools.release.prepare_release_candidate import verify_bundle

    bundle = Path(bundle).resolve()
    output, folder = images._fresh_output(output)
    require(not output.resolve().is_relative_to(bundle) and not folder.resolve().is_relative_to(bundle),
            'output must be outside the fixed bundle')
    manifest = verify_bundle(bundle)
    exported, events_digest = images._export(events, manifest=manifest)
    require(exported.get('mode') == 'webp-receiver' and exported.get('encoding') == 'w1', 'expected WebP receiver export')
    protocol, app = images._runtime(bundle / 'images', candidate=True)
    require(protocol.WEBP_VERSIONS == ('v5',), 'WebP capability differs')
    baseline = load_baseline(gallery)
    gallery_hash = hashlib.sha256((Path(gallery) / 'manifest.json').read_bytes()).hexdigest()
    posts = compare_posts(exported, baseline, protocol)
    transitions = [(row, attachment(row, protocol, entries=images._entries(row.get('entries'))))
                   for row in exported['transition_posts']]
    inputs = gallery_inputs(baseline)
    require(len(inputs) == 49, 'expected 49 approved distinct image inputs')
    output.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir()
    rendered, corpus, omitted, transition_results = {}, [], [], []
    with images._test_keys(), local_http(app) as request:
        health = request('/healthz')
        require(health['status'] == '200 OK' and health['body'] == b'ok\n', 'candidate health failed')
        for row in inputs:
            path = protocol.make_path(row['records'], KEY_ID, KEY, version='v5', encoding='w1')
            excluded = offline_reason(row, protocol.parse_path(path, {KEY_ID: KEY}))
            if excluded:
                omitted.append({'id': row['id'], 'usages': row['usages'], **excluded})
                continue
            print('HTTP WebP ' + row['id'], flush=True)
            response = request(path, accept='image/png')
            details = webp_response(response, row['png'])
            filename = row['id'] + '.webp'
            (folder / filename).write_bytes(response['body'])
            details.update(path=folder.name + '/' + filename, url=ORIGIN + path, png_sha256=row['png_sha256'])
            rendered[path] = details
            corpus.append({'id': row['id'], 'usages': row['usages'], **details})
        post_results = []
        for row, path in posts:
            require(path is None or path in rendered, 'real post URL has no verified HTTP image')
            post_results.append({'case': row['case'], 'number': row['number'], 'event_id': row['event']['id'],
                                 'url': row['url'], 'alt': row['alt'], 'image': rendered.get(path)})
        for number, (row, path) in enumerate(transitions, 1):
            if path and path not in rendered:
                require(protocol.path_encoding(path) == 'png', 'new WebP transition has no approved image')
                response = request(path)
                details = images._png_response(response, (1440, 1200))
                filename = f'transition-{number:02d}.png'
                (folder / filename).write_bytes(response['body'])
                details.update(path=folder.name + '/' + filename, url=ORIGIN + path, mime='image/png')
                rendered[path] = details
            transition_results.append({'label': row['label'], 'event_id': row['event']['id'],
                                       'url': row['url'], 'image': rendered.get(path)})
        representative = next(path for row, path in posts if row['case'] == 'nankai-advisory')
        contract = request_contract(request, representative, rendered[representative])
    images._runtime_origins(bundle / 'images')
    require(verify_bundle(bundle) == manifest, 'candidate changed during HTTP verification')
    result = {'schema_version': 1, 'mode': 'webp-http', 'candidate_version': 'v5', 'encoding': 'w1',
              'bundle_manifest_sha256': manifest['manifest_sha256'], 'receiver_export_sha256': events_digest,
              'gallery_manifest_sha256': gallery_hash, 'case_count': exported['case_count'],
              'post_count': len(post_results), 'posts': post_results, 'transition_posts': transition_results,
              'approved_unique_inputs': len(inputs), 'verified_http_inputs': len(corpus),
              'verified_gallery_usages': sum(len(row['usages']) for row in corpus),
              'corpus': corpus, 'offline_only_inputs': omitted,
              'checks': {'real_loopback_http': True, 'fixed_candidate': True, 'approved_post_inputs_text_alt': True,
                         'imeta_and_body': True, 'health': True, 'rgba_pixels_exact': True, **contract},
              'scope': 'Real Service/Nostr export and loopback HTTP; no deployment/CDN/TLS or external relay traffic.'}
    images._write_output(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('receiver', 'images'))
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--events', type=Path)
    parser.add_argument('--gallery', type=Path)
    args = parser.parse_args(argv)
    if args.phase == 'images' and (args.events is None or args.gallery is None):
        parser.error('images requires --events and --gallery')
    try:
        result = (check_receiver(args.bundle, args.output) if args.phase == 'receiver' else
                  check_images(args.bundle, args.events, args.gallery, args.output))
    except (OSError, ValueError, RuntimeError, ImportError, KeyError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps({'mode': result['mode'], 'posts': result['post_count'],
                      'transitions': len(result['transition_posts']), 'output': str(args.output.absolute())}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
