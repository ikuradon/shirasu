"""Run verified backups on an interval and retain only this worker's archives."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import stat
import tempfile
import threading
import time
import uuid

from backup import create_backup, verify_backup


logger = logging.getLogger('shirasu.backup')
ARCHIVE_NAME = re.compile(r'auto-\d{8}T\d{12}Z-[0-9a-f]{32}\.sqlite3')
HEARTBEAT_SECONDS = 30


@dataclass(frozen=True)
class Settings:
    state_path: str = 'state/shirasu.sqlite3'
    backup_dir: str = 'backups/auto'
    interval_seconds: int = 3600
    keep_count: int = 168
    retry_seconds: int = 60

    def __post_init__(self):
        for value in (self.interval_seconds, self.keep_count, self.retry_seconds):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError('Backup interval, retention and retry must be positive integers')
        if not self.state_path or not self.backup_dir:
            raise ValueError('State and backup paths must not be empty')

    @classmethod
    def load(cls, environ=None):
        env = os.environ if environ is None else environ
        return cls(
            state_path=env.get('SHIRASU_STATE_PATH', 'state/shirasu.sqlite3'),
            backup_dir=env.get('SHIRASU_BACKUP_DIR', 'backups/auto'),
            interval_seconds=int(env.get('SHIRASU_BACKUP_INTERVAL_SECONDS', '3600')),
            keep_count=int(env.get('SHIRASU_BACKUP_KEEP_COUNT', '168')),
        )


def _timestamp(value):
    try:
        return (isinstance(value, (float, int)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except OverflowError:
        return False


def _read_status(directory):
    try:
        with (Path(directory) / 'status.json').open(encoding='utf-8') as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            return None
        state = json.loads(raw)
        if not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != 1:
            return None
        if not isinstance(state.get('running'), bool) or not _timestamp(state.get('heartbeat')):
            return None
        for name in ('last_attempt', 'last_success', 'next_attempt_at'):
            if state.get(name) is not None and not _timestamp(state[name]):
                return None
        for name in ('last_file', 'last_error', 'retention_error'):
            if state.get(name) is not None and not isinstance(state[name], str):
                return None
        for name in ('interval_seconds', 'keep_count'):
            if type(state.get(name)) is not int or state[name] <= 0:
                return None
        return state
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _archive_path(directory, name):
    if not isinstance(name, str) or ARCHIVE_NAME.fullmatch(name) is None:
        return None
    path = Path(directory) / name
    try:
        return path if stat.S_ISREG(path.lstat().st_mode) else None
    except OSError:
        return None


def healthy(settings, now=None):
    """Check saved worker state without creating files or taking a writer lock."""
    now = time.time() if now is None else now
    state = _read_status(settings.backup_dir)
    if state is None or not _timestamp(now):
        return False
    success = state.get('last_success')
    return bool(
        state['running'] and 0 <= now - state['heartbeat'] <= 90
        and _timestamp(success) and 0 <= now - success <= settings.interval_seconds + 120
        and (state.get('last_attempt') is None or state['last_attempt'] <= now)
        and not state.get('last_error') and not state.get('retention_error')
        and _archive_path(settings.backup_dir, state.get('last_file')) is not None
    )


@contextmanager
def worker_lock(directory):
    descriptor = os.open(Path(directory) / '.backup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another backup worker is using this directory') from error
        yield
    finally:
        os.close(descriptor)


def _error_text(error):
    message = ' '.join(''.join(char if char.isprintable() else ' ' for char in str(error)).split())
    return f'{type(error).__name__}: {message}'[:1000]


class Worker:
    def __init__(self, settings, *, clock=time.time, monotonic=time.monotonic, stop_event=None):
        self.settings = settings
        self.directory = Path(settings.backup_dir)
        self.clock = clock
        self.monotonic = monotonic
        self.stop = stop_event if stop_event is not None else threading.Event()
        self.state = {
            'version': 1, 'running': True, 'heartbeat': clock(),
            'last_attempt': None, 'last_success': None, 'last_file': None,
            'last_error': None, 'retention_error': None,
            'interval_seconds': settings.interval_seconds, 'keep_count': settings.keep_count,
            'next_attempt_at': None,
        }
        self.deadline = monotonic()

    def save_status(self):
        self.state['heartbeat'] = self.clock()
        descriptor, name = tempfile.mkstemp(prefix='.status-', dir=self.directory)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump(self.state, handle, ensure_ascii=True, allow_nan=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.directory / 'status.json')
        finally:
            Path(name).unlink(missing_ok=True)

    def schedule(self, delay):
        self.deadline = self.monotonic() + delay
        self.state['next_attempt_at'] = self.clock() + delay

    def recover_schedule(self):
        """Only a verified recent success may defer the first backup after restart."""
        previous = _read_status(self.directory)
        now = self.clock()
        self.schedule(0)
        if previous is None:
            return
        for name in ('heartbeat', 'last_attempt', 'last_success'):
            value = previous.get(name)
            if value is not None and value > now:
                return
        for name in ('last_attempt', 'last_success', 'last_file', 'last_error', 'retention_error'):
            self.state[name] = previous.get(name)
        success = self.state['last_success']
        path = _archive_path(self.directory, self.state['last_file'])
        if not _timestamp(success) or path is None:
            self.state['last_success'] = None
            return
        try:
            summary = verify_backup(path)
            if not 0 <= summary['backup_created_at'] <= success <= now:
                raise ValueError('Saved backup timestamps are inconsistent')
        except Exception as error:
            self.state['last_success'] = None
            self.state['last_error'] = _error_text(error)
            return
        if not self.state['last_error']:
            self.schedule(max(0, success + self.settings.interval_seconds - now))

    def prune(self, current):
        # The just-verified archive is always retained, even after clock rollback.
        source = Path(self.settings.state_path).resolve()
        candidates = [path for path in self.directory.iterdir()
                      if path != current and _archive_path(self.directory, path.name) is not None
                      and path.resolve() != source]
        candidates.sort(key=lambda path: path.name, reverse=True)
        removed = 0
        for path in candidates[max(0, self.settings.keep_count - 1):]:
            if _archive_path(self.directory, path.name) is not None:
                path.unlink()
                removed += 1
        return removed

    def run_backup(self):
        self.state['last_attempt'] = self.clock()
        self.save_status()
        stamp = datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        destination = self.directory / f'auto-{stamp}-{uuid.uuid4().hex}.sqlite3'
        try:
            summary = create_backup(self.settings.state_path, destination)
        except Exception as error:
            self.state['last_error'] = _error_text(error)
            self.schedule(self.settings.retry_seconds)
            logger.warning('Backup failed; retry_in=%ss reason=%s',
                           self.settings.retry_seconds, self.state['last_error'])
        else:
            self.state.update(last_success=self.clock(), last_file=destination.name,
                              last_error=None, retention_error=None)
            self.schedule(self.settings.interval_seconds)
            self.save_status()
            logger.info('Backup verified file=%s events=%s', destination.name, summary['events_verified'])
            try:
                removed = self.prune(destination)
                if removed:
                    logger.info('Removed old automatic backups count=%s', removed)
            except Exception as error:
                self.state['retention_error'] = _error_text(error)
                logger.warning('Backup retained; cleanup failed reason=%s', self.state['retention_error'])
        self.save_status()

    def run(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with worker_lock(self.directory):
            self.recover_schedule()
            self.save_status()
            logger.info('Backup worker started interval=%ss keep=%s',
                        self.settings.interval_seconds, self.settings.keep_count)
            try:
                while not self.stop.is_set():
                    remaining = self.deadline - self.monotonic()
                    if remaining <= 0:
                        self.run_backup()
                    else:
                        self.stop.wait(min(HEARTBEAT_SECONDS, remaining))
                        self.save_status()
            finally:
                self.state['running'] = False
                self.save_status()
                logger.info('Backup worker stopped')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--health', action='store_true', help='silently check the saved backup worker status')
    args = parser.parse_args(argv)
    try:
        settings = Settings.load()
        if args.health:
            return 0 if healthy(settings) else 1
        logging.basicConfig(level=os.environ.get('SHIRASU_LOG_LEVEL', 'INFO'),
                            format='%(asctime)s %(levelname)s %(name)s %(message)s')
        worker = Worker(settings)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda signum, frame: worker.stop.set())
        worker.run()
        return 0
    except Exception as error:
        if not args.health:
            logger.error('Backup worker stopped with an error: %s', _error_text(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
