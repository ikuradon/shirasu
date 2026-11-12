import io
import socket
import threading
import unittest
from unittest.mock import Mock, patch

import azarashi

from shirasu.receiver import GpsdReceiver


class StopFlag:
    """No sleeping: record reconnect waits and let each test control shutdown."""

    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, delay):
        self.waits.append(delay)
        return self.stopped


class FakeSocket:
    def __init__(self):
        self.stream = io.BytesIO()
        self.close = Mock()
        self.shutdown = Mock()
        self.settimeout = Mock()
        self.sendall = Mock()

    def makefile(self, mode):
        if mode != 'rb':
            raise AssertionError('The gpsd stream must be binary')
        return self.stream


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.stop = StopFlag()
        self.reports = Mock()
        self.state = Mock()
        self.receiver = GpsdReceiver('gpsd.example.test', 2947, self.reports,
                                     self.state, self.stop)
        self.enterContext(patch('shirasu.receiver.logger'))

    def deliver_then_stop(self, stream, **kwargs):
        self.assertFalse(kwargs['unique'])
        self.assertFalse(kwargs['ignore_dcx'])
        self.assertEqual(kwargs['msg_type'], 'ublox')
        kwargs['callback']('report')
        self.stop.set()

    def test_connection_refusal_retries_and_recovers(self):
        sock = FakeSocket()
        with patch('shirasu.receiver.socket.create_connection', side_effect=[
            ConnectionRefusedError('not running'), sock,
        ]) as connect, patch('shirasu.receiver.azarashi.decode_stream', self.deliver_then_stop):
            self.receiver.run()

        self.assertEqual(connect.call_count, 2)
        self.assertEqual(self.stop.waits, [1])
        self.reports.assert_called_once_with('report')
        sock.settimeout.assert_called_once_with(30)
        sock.sendall.assert_called_once_with(b'?WATCH={"enable":true,"raw":2};\n')
        self.assertTrue(sock.stream.closed)
        sock.close.assert_called_once()
        self.assertIn((True, None), [call.args for call in self.state.call_args_list])

    def test_eof_reconnects_with_a_new_stream(self):
        first, second = FakeSocket(), FakeSocket()
        seen = []

        def read(stream, **kwargs):
            seen.append(stream)
            if stream is first.stream:
                raise azarashi.AzarashiNoMoreData('gpsd closed the connection')
            self.deliver_then_stop(stream, **kwargs)

        with patch('shirasu.receiver.socket.create_connection', side_effect=[first, second]), \
             patch('shirasu.receiver.azarashi.decode_stream', read):
            self.receiver.run()

        self.assertEqual(seen, [first.stream, second.stream])
        self.assertEqual(self.stop.waits, [1])
        self.assertTrue(first.stream.closed)
        first.close.assert_called_once()

    def test_invalid_frame_continues_on_the_same_stream(self):
        sock = FakeSocket()
        seen = []

        def read(stream, **kwargs):
            seen.append(stream)
            if len(seen) == 1:
                raise azarashi.AzarashiInvalidMessageError('bad checksum')
            self.deliver_then_stop(stream, **kwargs)

        with patch('shirasu.receiver.socket.create_connection', return_value=sock) as connect, \
             patch('shirasu.receiver.azarashi.decode_stream', read):
            self.receiver.run()

        connect.assert_called_once()
        self.assertEqual(seen, [sock.stream, sock.stream])
        self.assertEqual(self.stop.waits, [])

    def test_timeouts_discard_the_poisoned_stream(self):
        for error in (TimeoutError('read timed out'),
                      azarashi.AzarashiTimeoutError('partial message'),
                      azarashi.AzarashiDisconnectedError('socket unusable')):
            with self.subTest(error=type(error).__name__):
                self.stop = StopFlag()
                self.receiver = GpsdReceiver('gpsd.example.test', 2947, self.reports,
                                             self.state, self.stop)
                first, second = FakeSocket(), FakeSocket()
                seen = []

                def read(stream, **kwargs):
                    seen.append(stream)
                    if stream is first.stream:
                        raise error
                    self.deliver_then_stop(stream, **kwargs)

                with patch('shirasu.receiver.socket.create_connection', side_effect=[first, second]), \
                     patch('shirasu.receiver.azarashi.decode_stream', read):
                    self.receiver.run()
                self.assertEqual(seen, [first.stream, second.stream])
                self.assertTrue(first.stream.closed)

    def test_callback_io_errors_are_fatal_instead_of_retried(self):
        sock = FakeSocket()
        failure = OSError('cannot persist received report')
        self.reports.side_effect = failure

        with patch('shirasu.receiver.socket.create_connection', return_value=sock) as connect, \
             patch('shirasu.receiver.azarashi.decode_stream', self.deliver_then_stop):
            with self.assertRaises(OSError) as raised:
                self.receiver.run()

        self.assertIs(raised.exception, failure)
        connect.assert_called_once()
        self.assertEqual(self.stop.waits, [])
        self.assertTrue(sock.stream.closed)
        sock.close.assert_called_once()

    def test_decoder_configuration_errors_are_fatal(self):
        sock = FakeSocket()
        failure = azarashi.AzarashiUnsupportedFormatError('wrong format')
        with patch('shirasu.receiver.socket.create_connection', return_value=sock) as connect, \
             patch('shirasu.receiver.azarashi.decode_stream', side_effect=failure):
            with self.assertRaises(azarashi.AzarashiUnsupportedFormatError) as raised:
                self.receiver.run()
        self.assertIs(raised.exception, failure)
        connect.assert_called_once()
        self.assertEqual(self.stop.waits, [])
        self.assertTrue(sock.stream.closed)

    def test_backoff_only_resets_after_receiving_a_report(self):
        sockets = [FakeSocket() for _ in range(5)]
        calls = 0

        def read(stream, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                kwargs['callback']('report before disconnect')
            if calls < 5:
                raise EOFError('closed')
            self.stop.set()

        with patch('shirasu.receiver.socket.create_connection', side_effect=sockets), \
             patch('shirasu.receiver.azarashi.decode_stream', read):
            self.receiver.run()

        self.assertEqual(self.stop.waits, [1, 2, 1, 2])

    def test_backoff_is_capped_at_one_minute(self):
        sockets = [FakeSocket() for _ in range(9)]
        calls = 0

        def read(stream, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 9:
                raise EOFError('closed')
            self.stop.set()

        with patch('shirasu.receiver.socket.create_connection', side_effect=sockets), \
             patch('shirasu.receiver.azarashi.decode_stream', read):
            self.receiver.run()
        self.assertEqual(self.stop.waits, [1, 2, 4, 8, 16, 32, 60, 60])

    def test_close_interrupts_an_active_read(self):
        sock = FakeSocket()
        reading = threading.Event()
        released = threading.Event()
        self.stop = threading.Event()
        self.receiver = GpsdReceiver('gpsd.example.test', 2947, self.reports,
                                     self.state, self.stop)
        sock.shutdown.side_effect = lambda how: released.set()
        errors = []

        def read(stream, **kwargs):
            reading.set()
            if not released.wait(2):
                raise AssertionError('close did not unblock the socket read')
            raise EOFError('read unblocked by shutdown')

        def run():
            try:
                self.receiver.run()
            except BaseException as error:
                errors.append(error)

        with patch('shirasu.receiver.socket.create_connection', return_value=sock), \
             patch('shirasu.receiver.azarashi.decode_stream', read):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(reading.wait(2))
            finally:
                self.receiver.close()
                released.set()
                worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        sock.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
