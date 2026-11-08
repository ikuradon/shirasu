"""Expose saved delivery state without relying on the receiver process."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import math
import os
import signal
import threading
import time
from urllib.parse import urlsplit

from main import healthy
from outbox import read_status


logger = logging.getLogger('shirasu.metrics')


METRICS = {
    'state_readable': ('gauge', 'Whether the saved state could be read.'),
    'healthy': ('gauge', 'Whether the saved state passes the application health check.'),
    'process_running': ('gauge', 'Whether the receiver last recorded itself as running.'),
    'heartbeat_timestamp_seconds': ('gauge', 'Unix time of the last receiver heartbeat.'),
    'last_received_timestamp_seconds': ('gauge', 'Unix time of the last decoded report.'),
    'last_published_timestamp_seconds': ('gauge', 'Unix time of the last acknowledged publication.'),
    'gpsd_connected': ('gauge', 'Whether the receiver last recorded a GPSD connection.'),
    'relay_connected': ('gauge', 'Whether the receiver last recorded a relay connection.'),
    'pending_reports': ('gauge', 'Reports waiting for acknowledged delivery.'),
    'oldest_pending_age_seconds': ('gauge', 'Age of the oldest pending report; zero when empty.'),
    'delivery_failed': ('gauge', 'Whether an unresolved delivery error is recorded.'),
    'dropped_reports_total': ('counter', 'Reports that could not be saved because the outbox was full.'),
    'expired_reports': ('gauge', 'Expired reports still retained in the outbox.'),
}


def number(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('Invalid saved metric value')
    return value


class Collector:
    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        self._error = None
        self._lock = threading.Lock()

    def collect(self):
        values = {'state_readable': 0, 'healthy': 0}
        try:
            now = self.clock()
            snapshot = read_status(self.path, now)
            state = snapshot['status']
            values = {
                'state_readable': 1,
                'healthy': int(healthy(snapshot, now)),
                'process_running': int(bool(state.get('running'))),
                'heartbeat_timestamp_seconds': number(state.get('heartbeat', 0)),
                'last_received_timestamp_seconds': number(state.get('last_received', 0)),
                'last_published_timestamp_seconds': number(state.get('last_published', 0)),
                'gpsd_connected': int(bool(state.get('gpsd_connected'))),
                'relay_connected': int(bool(state.get('relay_connected'))),
                'pending_reports': number(snapshot['counts']['pending']),
                'oldest_pending_age_seconds': number(snapshot['oldest_pending_age_seconds'] or 0),
                'delivery_failed': int(bool(state.get('last_delivery_error'))),
                'dropped_reports_total': number(state.get('dropped_full', 0)),
                'expired_reports': number(snapshot['counts']['expired']),
            }
        except Exception as error:
            # Keep the endpoint available so DB failure differs from exporter failure.
            # Do not expose paths, exception messages, identities or report contents.
            error_name = type(error).__name__
            with self._lock:
                if self._error != error_name:
                    logger.warning('Saved state unavailable: %s', error_name)
                self._error = error_name
            return {'state_readable': 0, 'healthy': 0}
        with self._lock:
            if self._error is not None:
                logger.info('Saved state is readable again')
            self._error = None
        return values

    def render(self):
        lines = []
        for name, value in self.collect().items():
            kind, description = METRICS[name]
            name = 'shirasu_' + name
            lines.extend((f'# HELP {name} {description}', f'# TYPE {name} {kind}',
                          f'{name} {value:.17g}'))
        return ('\n'.join(lines) + '\n').encode('utf-8')


class MetricsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, collector):
        self.collector = collector
        super().__init__(address, MetricsHandler)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(5)
        return sock, address


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond(include_body=True)

    def do_HEAD(self):
        self.respond(include_body=False)

    def respond(self, include_body):
        path = urlsplit(self.path).path
        content_type = 'text/plain; charset=utf-8'
        if path == '/metrics':
            status = 200
            body = self.server.collector.render()
            content_type = 'text/plain; version=0.0.4; charset=utf-8'
        elif path == '/healthz':
            # This probes the exporter itself; application health is a metric.
            status, body = 200, b'ok\n'
        else:
            status, body = 404, b'not found\n'
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def log_message(self, format, *args):
        # Routine scrapes should not consume Docker log space.
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=9016)
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get('SHIRASU_LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    path = os.environ.get('SHIRASU_STATE_PATH', 'state/shirasu.sqlite3')

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    with MetricsServer((args.host, args.port), Collector(path)) as server:
        logger.info('Metrics listening on %s:%s', args.host, server.server_port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
