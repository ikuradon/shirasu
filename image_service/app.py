"""WSGI entrypoint for Vercel or a local WSGI server; PNGs are never stored."""

import hashlib
from importlib import import_module
import os

from image_protocol import MAX_PATH_LENGTH, SUPPORTED_VERSIONS, load_keys, parse_path, path_version

RENDERERS = {'v1': 'image_service.render', 'v2': 'image_service.render_v2',
             'v3': 'image_service.render_v3', 'v4': 'image_service.render_v4'}


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
        keys = load_keys(os.environ.get('SHIRASU_IMAGE_KEYS', ''))
    except ValueError:
        return error('503 Service Unavailable', 'image service is not configured')
    if path == '/healthz':
        try:
            for module in RENDERERS.values():
                import_module(module).verify_assets()
        except Exception:
            return error('503 Service Unavailable', 'renderer is unavailable')
        return respond('200 OK', b'ok\n', [('Content-Type', 'text/plain'), ('Cache-Control', 'no-store')])
    try:
        records = parse_path(path, keys)
    except ValueError:
        return error('403 Forbidden', 'invalid image URL')
    try:
        renderer = import_module(RENDERERS[path_version(path)])
        image = renderer.render(records)
    except (ValueError, OverflowError):
        return error('400 Bad Request', 'invalid report bundle')
    except Exception:
        # Never reflect signed payloads, NMEA text, key IDs or secrets in errors.
        return error('503 Service Unavailable', 'renderer is unavailable')
    tag = '"' + hashlib.sha256(image.png).hexdigest() + '"'
    headers = [('Content-Type', 'image/png'), ('X-Content-Type-Options', 'nosniff'),
               ('Cache-Control', 'public, max-age=31536000, immutable'),
               ('Vercel-CDN-Cache-Control', 'public, max-age=31536000'),
               ('ETag', tag)]
    if environ.get('HTTP_IF_NONE_MATCH') == tag:
        start_response('304 Not Modified', headers)
        return []
    return respond('200 OK', image.png, headers)
