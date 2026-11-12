"""WSGI entrypoint for Vercel or a local WSGI server; images are never stored."""

import hashlib
from importlib import import_module
import os

from shirasu.images.protocol import MAX_PATH_LENGTH, SUPPORTED_VERSIONS, load_keys, parse_path, path_encoding, path_version
from shirasu.images.versions import RENDERERS, WEBP_VERSIONS


# Deployment tooling checks this protocol marker before injecting auxiliary keys.
IMAGE_CHECK_KEYS_VERSION = 1


def _runtime_keys():
    # The receiver's normal keyring remains mandatory. A deployment may add
    # random CI-only keys without exposing or replacing those publishing keys.
    keys = load_keys(os.environ.get('SHIRASU_IMAGE_KEYS', ''))
    check_config = os.environ.get('SHIRASU_IMAGE_CHECK_KEYS')
    if check_config is not None:
        check_keys = load_keys(check_config)
        if any(not key_id.startswith('check-') or key_id in keys for key_id in check_keys):
            raise ValueError('Invalid image check key configuration')
        keys.update(check_keys)
    return keys


def app(environ, start_response):
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '')

    def respond(status, data, headers=()):
        start_response(status, [('Content-Length', str(len(data))), *headers])
        return [] if method == 'HEAD' else [data]

    def error(status, message, extra=()):
        return respond(status, (message + '\n').encode(),
                       [('Content-Type', 'text/plain; charset=utf-8'), ('Cache-Control', 'no-store'), *extra])

    if method not in ('GET', 'HEAD'):
        return error('405 Method Not Allowed', 'method not allowed', [('Allow', 'GET, HEAD')])
    if environ.get('QUERY_STRING'):
        return error('400 Bad Request', 'query parameters are not supported')
    if len(path) > MAX_PATH_LENGTH:
        return error('414 URI Too Long', 'image URL is too long')
    if path != '/healthz' and not path.startswith(tuple(f'/{version}/' for version in SUPPORTED_VERSIONS)):
        return error('404 Not Found', 'not found')
    try:
        keys = _runtime_keys()
    except ValueError:
        return error('503 Service Unavailable', 'image service is not configured')
    if path == '/healthz':
        try:
            for version, module in RENDERERS.items():
                renderer = import_module(module)
                renderer.verify_assets()
                if version in WEBP_VERSIONS:
                    renderer.verify_webp_encoder()
        except Exception:
            return error('503 Service Unavailable', 'renderer is unavailable')
        return respond('200 OK', b'ok\n', [('Content-Type', 'text/plain'), ('Cache-Control', 'no-store')])
    try:
        records = parse_path(path, keys)
    except ValueError:
        return error('403 Forbidden', 'invalid image URL')
    try:
        renderer = import_module(RENDERERS[path_version(path)])
        encoding = path_encoding(path)
        if encoding == 'png':
            image = renderer.render(records)
            data, mime = image.png, 'image/png'
        else:
            image = renderer.render_webp(records)
            data, mime = image.data, image.mime
            # An encoder failure or wrong result must never publish a PNG (or
            # a truncated/different format) under an immutable WebP URL.
            if (mime != 'image/webp' or not isinstance(data, bytes) or len(data) < 20
                    or data[:4] != b'RIFF' or data[8:12] != b'WEBP'
                    or int.from_bytes(data[4:8], 'little') != len(data) - 8
                    or data[12:16] not in (b'VP8L', b'VP8X')):
                raise RuntimeError('Unexpected WebP encoder result')
    except (ValueError, OverflowError):
        return error('400 Bad Request', 'invalid report bundle')
    except Exception:
        # Never reflect signed payloads, NMEA text, key IDs or secrets in errors.
        return error('503 Service Unavailable', 'renderer is unavailable')
    tag = '"' + hashlib.sha256(data).hexdigest() + '"'
    headers = [('Content-Type', mime), ('X-Content-Type-Options', 'nosniff'),
               ('Cache-Control', 'public, max-age=31536000, immutable'),
               ('Vercel-CDN-Cache-Control', 'public, max-age=31536000'),
               ('ETag', tag)]
    if environ.get('HTTP_IF_NONE_MATCH') == tag:
        start_response('304 Not Modified', headers)
        return []
    return respond('200 OK', data, headers)
