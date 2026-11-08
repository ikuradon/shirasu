import http.client
from pathlib import Path
import tempfile
import threading
import unittest

from metrics import Collector, MetricsServer
from outbox import Outbox


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite3'
        self.now = 1000
        self.collector = Collector(self.path, clock=lambda: self.now)

    def open(self):
        box = Outbox(self.path, 'private-account-identifier')
        self.addCleanup(box.close)
        for name, value in {
            'running': True, 'gpsd_connected': True, 'relay_connected': True,
            'heartbeat': self.now, 'last_received': self.now,
        }.items():
            box.set_status(name, value, self.now)
        return box

    def test_missing_database_is_visible_and_is_not_created(self):
        with self.assertLogs('shirasu.metrics', level='WARNING'):
            self.assertEqual(self.collector.collect(), {'state_readable': 0, 'healthy': 0})
        self.assertFalse(self.path.exists())

    def test_corrupt_database_is_visible_without_exposing_error_details(self):
        self.path.write_bytes(b'not a sqlite database')
        with self.assertLogs('shirasu.metrics', level='WARNING'):
            text = self.collector.render().decode()
        self.assertIn('shirasu_state_readable 0\n', text)
        self.assertNotIn(str(self.path), text)
        self.assertNotIn('not a sqlite database', text)

    def test_reception_delivery_and_storage_failures_are_separate_metrics(self):
        box = self.open()
        box.enqueue('warning', '{"content":"private-report-body"}', 'event-id', self.now - 20)
        box.set_status('last_delivery_error', 'secret-webhook-token', self.now)
        box.set_status('dropped_full', 4, self.now)
        values = self.collector.collect()
        self.assertEqual(values['state_readable'], 1)
        self.assertEqual(values['healthy'], 0)
        self.assertEqual(values['pending_reports'], 1)
        self.assertEqual(values['oldest_pending_age_seconds'], 20)
        self.assertEqual(values['delivery_failed'], 1)
        self.assertEqual(values['dropped_reports_total'], 4)
        text = self.collector.render().decode()
        for sensitive in ('private-account-identifier', 'private-report-body',
                          'secret-webhook-token', 'event-id'):
            self.assertNotIn(sensitive, text)
        self.assertIn('# TYPE shirasu_dropped_reports_total counter\n', text)
        self.assertTrue(text.endswith('\n'))

    def test_stale_heartbeat_is_unhealthy_even_if_receiver_saved_running(self):
        box = self.open()
        self.assertEqual(self.collector.collect()['healthy'], 1)
        box.close()
        self.now += 90
        values = self.collector.collect()
        self.assertEqual(values['state_readable'], 1)
        self.assertEqual(values['process_running'], 1)
        self.assertEqual(values['healthy'], 0)
        self.assertEqual(values['heartbeat_timestamp_seconds'], 1000)

    def test_database_recovery_and_processing_recovery_are_observed(self):
        with self.assertLogs('shirasu.metrics', level='WARNING'):
            self.collector.collect()
        box = self.open()
        with self.assertLogs('shirasu.metrics', level='INFO'):
            self.assertEqual(self.collector.collect()['healthy'], 1)
        box.set_status('running', False, self.now)
        self.assertEqual(self.collector.collect()['process_running'], 0)
        box.set_status('running', True, self.now)
        self.assertEqual(self.collector.collect()['healthy'], 1)

    def test_scrape_does_not_change_saved_state(self):
        box = self.open()
        before = box.snapshot(self.now)
        self.collector.collect()
        self.collector.render()
        self.assertEqual(box.snapshot(self.now), before)

    def test_epoch_timestamps_keep_submillisecond_precision(self):
        self.now = 1794148017.0343413
        self.open()
        samples = dict(line.split() for line in self.collector.render().decode().splitlines()
                       if not line.startswith('#'))
        for name in ('heartbeat_timestamp_seconds', 'last_received_timestamp_seconds'):
            self.assertLess(abs(float(samples['shirasu_' + name]) - self.now), 0.001)

    def test_http_metrics_health_and_head_work_while_database_is_missing(self):
        with MetricsServer(('127.0.0.1', 0), self.collector) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for method, path, expected in [('GET', '/metrics', 200),
                                                ('HEAD', '/metrics', 200),
                                                ('GET', '/healthz', 200),
                                                ('GET', '/missing', 404)]:
                    with self.subTest(method=method, path=path):
                        connection = http.client.HTTPConnection('127.0.0.1', server.server_port,
                                                                timeout=2)
                        try:
                            connection.request(method, path)
                            response = connection.getresponse()
                            body = response.read()
                            self.assertEqual(response.status, expected)
                            if method == 'HEAD':
                                self.assertEqual(body, b'')
                            elif path == '/metrics':
                                self.assertIn(b'shirasu_state_readable 0\n', body)
                                self.assertIn('version=0.0.4', response.getheader('Content-Type'))
                            elif path == '/healthz':
                                self.assertEqual(body, b'ok\n')
                        finally:
                            connection.close()
            finally:
                server.shutdown()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
