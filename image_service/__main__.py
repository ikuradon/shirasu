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
    parser.add_argument('--version', choices=('v1', 'v2'), default='v1', help='renderer for an offline preview')
    args = parser.parse_args()
    if args.serve:
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
        if args.version == 'v1':
            from image_service.render import render
        else:
            from image_service.render_v2 import render
        records = json.loads(args.records.read_text())
        image = render([(row['nmea'], row['received_ms']) for row in records])
        with args.output.open('xb') as destination:
            destination.write(image.png)
        print(f'{image.category}: {args.output}')


if __name__ == '__main__':
    main()
