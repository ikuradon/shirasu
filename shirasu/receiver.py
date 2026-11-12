"""Blocking gpsd reader intended to run in a dedicated worker thread."""

import logging
import socket
import threading

import azarashi


logger = logging.getLogger(__name__)


class _Stopped(Exception):
    pass


class _CallbackFailure(Exception):
    """Keep application failures distinct from errors raised by the socket."""

    def __init__(self, error):
        self.error = error
        self.traceback = error.__traceback__


class GpsdReceiver:
    def __init__(self, host, port, on_report, on_state, stop_event,
                 read_timeout=30, connect_timeout=5):
        self.host = host
        self.port = port
        self.on_report = on_report
        self.on_state = on_state
        self.stop_event = stop_event
        self.read_timeout = read_timeout
        self.connect_timeout = connect_timeout
        self._socket = None
        self._socket_lock = threading.Lock()
        self._retry_delay = 1

    def _state(self, connected, error=None):
        try:
            self.on_state(connected, error)
        except BaseException as error:
            raise _CallbackFailure(error) from error

    def _report(self, report):
        if self.stop_event.is_set():
            raise _Stopped()
        try:
            self.on_report(report)
        except BaseException as error:
            raise _CallbackFailure(error) from error
        # A TCP connection alone does not mean the receiver is producing data.
        self._retry_delay = 1

    def _read_connection(self):
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        with self._socket_lock:
            self._socket = sock
        try:
            if self.stop_event.is_set():
                raise _Stopped()
            sock.settimeout(self.read_timeout)
            sock.sendall(b'?WATCH={"enable":true,"raw":2};\n')
            with sock.makefile('rb') as stream:
                self._state(True)
                logger.info('GPSD connected to %s:%s', self.host, self.port)
                while not self.stop_event.is_set():
                    try:
                        azarashi.decode_stream(
                            stream, msg_type='ublox', callback=self._report,
                            unique=False, ignore_dcx=False,
                        )
                    except azarashi.AzarashiTimeoutError:
                        # A socket.makefile() cannot be reused after a timeout.
                        raise
                    except azarashi.AzarashiReadOn as error:
                        logger.warning('GPSD frame skipped: %s: %s',
                                       type(error).__name__, error.message)
        finally:
            with self._socket_lock:
                if self._socket is sock:
                    self._socket = None
            sock.close()

    def run(self):
        """Read until stopped, reconnecting only for transport/stream failures.

        on_report runs synchronously. It may wait for durable storage in the main
        thread; its exceptions must terminate this worker instead of dropping data.
        """
        try:
            while not self.stop_event.is_set():
                try:
                    self._read_connection()
                except _Stopped:
                    break
                except (OSError, EOFError, azarashi.AzarashiTimeoutError) as error:
                    if self.stop_event.is_set():
                        break
                    reason = f'{type(error).__name__}: {error}'
                    self._state(False, reason)
                    delay = self._retry_delay
                    logger.warning('GPSD disconnected; retrying in %ss: %s', delay, reason)
                    if self.stop_event.wait(delay):
                        break
                    self._retry_delay = min(delay * 2, 60)
            self._state(False)
        except _CallbackFailure as failure:
            raise failure.error.with_traceback(failure.traceback) from None

    def close(self):
        """Interrupt a blocking read and prevent further reconnect attempts."""
        self.stop_event.set()
        with self._socket_lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                # The read thread may have closed the socket in the meantime.
                pass
