"""Offline previews and a localhost-only WSGI development server."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--records', type=Path, help='JSON array of {nmea, received_ms} records')
    group.add_argument('--serve', action='store_true', help='serve on 127.0.0.1; requires SHIRASU_IMAGE_KEYS')
    parser.add_argument('--output', type=Path, help='new PNG output path for an offline preview')
    parser.add_argument('--port', type=int, default=8080)
    rendering = parser.add_mutually_exclusive_group()
    rendering.add_argument('--version', choices=('v1', 'v2', 'v3', 'v4'), default=None,
                           help='published renderer for offline output (default: v1)')
    rendering.add_argument('--preview', action='store_true', help='use unpublished offline renderer')
    parser.add_argument('--cancel-reference', type=Path,
                        help='independently confirmed original records JSON for a cancellation; --preview only')
    args = parser.parse_args()
    if args.cancel_reference and (not args.preview or not args.records):
        parser.error('--cancel-reference requires --preview and --records; confirm the original independently')
    if args.serve:
        if args.preview:
            parser.error('--preview requires --records; unpublished images are offline only')
        if args.output:
            parser.error('--output requires --records')
        from wsgiref.simple_server import make_server, WSGIRequestHandler
        from image_service.app import app

        class QuietHandler(WSGIRequestHandler):
            def log_message(self, format, *args):
                # Signed URLs contain report payloads; omit access logging.
                pass

        with make_server('127.0.0.1', args.port, app, handler_class=QuietHandler) as server:
            print(f'Image service listening on http://127.0.0.1:{server.server_port}', flush=True)
            server.serve_forever()
    else:
        if not args.output:
            parser.error('--records requires --output')
        if args.preview:
            from importlib.util import find_spec
            if find_spec('image_service.render_preview') is None:
                parser.error('Preview sources are not included in this package')
            from image_service.render_preview import render
        elif args.version in (None, 'v1'):
            from image_service.render import render
        elif args.version == 'v2':
            from image_service.render_v2 import render
        elif args.version == 'v3':
            from image_service.render_v3 import render
        else:
            from image_service.render_v4 import render
        records = json.loads(args.records.read_text())
        options = {}
        if args.cancel_reference:
            reference = json.loads(args.cancel_reference.read_text())
            options['cancel_reference'] = [(row['nmea'], row['received_ms']) for row in reference]
        image = render([(row['nmea'], row['received_ms']) for row in records], **options)
        with args.output.open('xb') as destination:
            destination.write(image.png)
        print(f'{image.category}: {args.output}')


if __name__ == '__main__':
    main()
