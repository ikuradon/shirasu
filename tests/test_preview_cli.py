"""Exercise the preview CLI in isolated processes with side effects forbidden."""

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

import azarashi

from main import report_message
from outbox import Outbox
from test_dcx_instructions import evacuation_report


ROOT = Path(__file__).resolve().parents[1]
DCR_NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
MARINE_NMEA = '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B'
NULL_NMEA = '$QZQSM,55,53B0000DE000000000000000000000000000000000000000000000000E41D10*04'
UNKNOWN_NMEA = '$QZQSM,55,53B0000DFF00000000000000000000000000000000000000000000000B82B78*71'
RECEIVED_AT = datetime(2026, 11, 8, tzinfo=timezone.utc)


def ubx_frame(sentence):
    data = bytes.fromhex(sentence.split(',')[2].split('*')[0] + '0')
    payload = bytes((5, 1, 1, 0, 8, 0, 2, 0))
    payload += b''.join(data[offset:offset + 4][::-1] for offset in range(0, 32, 4))
    message = b'\xb5\x62\x02\x13' + struct.pack('<H', len(payload)) + payload
    a = b = 0
    for value in message[2:]:
        a = (a + value) & 255
        b = (b + a) & 255
    return message + bytes((a, b))


GUARDED_CLI = '''
import sqlite3
from unittest.mock import patch
import main

def forbidden(*args, **kwargs):
    raise AssertionError('PREVIEW_SIDE_EFFECT: config, signing, network or storage access')

with patch.object(main, 'Nostr', side_effect=forbidden), \\
     patch.object(main, 'GpsdReceiver', side_effect=forbidden), \\
     patch.object(main, 'Outbox', side_effect=forbidden), \\
     patch.object(main, 'read_status', side_effect=forbidden), \\
     patch.object(main, 'read_history', side_effect=forbidden), \\
     patch.object(main.Keys, 'parse', side_effect=forbidden), \\
     patch.object(main.Keys, 'generate', side_effect=forbidden), \\
     patch.object(main.EventBuilder, 'finalize', side_effect=forbidden), \\
     patch.object(sqlite3, 'connect', side_effect=forbidden), \\
     patch('socket.create_connection', side_effect=forbidden), \\
     patch('socket.socket.connect', side_effect=forbidden):
    raise SystemExit(main.main())
'''


class PreviewCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-preview-cli-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ('main.py', 'outbox.py', 'receiver.py', 'inspection.py', 'preview.py', 'message_format.py',
                     'dcx_instructions.py', 'image_publication.py', 'image_protocol.py', 'typhoon.py', 'nankai.py'):
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / 'config.py').write_text(
            "raise AssertionError('PREVIEW_SIDE_EFFECT: config was imported')\n",
            encoding='utf-8',
        )
        (self.root / 'guarded_cli.py').write_text(GUARDED_CLI, encoding='utf-8')
        self.state_path = self.root / 'state.sqlite3'
        self.box = Outbox(self.state_path, 'test-account')
        self.addCleanup(self.box.close)
        # The event is deliberately overdue; preview must not inspect or expire it.
        self.box.enqueue('preserve', '{"content":"saved before preview"}', 'saved-event', 1000)
        self.box.set_status('running', True, 1000)

    def source(self, filename, content):
        path = self.root / filename
        path.write_bytes(content.encode('utf-8') if isinstance(content, str) else content)
        return path

    def expected(self, sentence):
        return report_message(azarashi.decode(sentence, timestamp=RECEIVED_AT))

    def run_cli(self, *arguments, data=None, absent_state=False):
        before = list(self.box.connection.iterdump())
        state_path = self.root / 'must-not-create' / 'state.sqlite3' if absent_state else self.state_path
        result = subprocess.run(
            [sys.executable, '-B', 'guarded_cli.py', *map(str, arguments)],
            cwd=self.root,
            env={'PATH': os.environ.get('PATH', ''), 'PYTHONUNBUFFERED': '1',
                 'PYTHONDONTWRITEBYTECODE': '1', 'SHIRASU_STATE_PATH': str(state_path)},
            input=data, capture_output=True, timeout=10,
        )
        result.stdout = result.stdout.decode('utf-8')
        result.stderr = result.stderr.decode('utf-8')
        self.assertNotIn('PREVIEW_SIDE_EFFECT', result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(list(self.box.connection.iterdump()), before)
        self.assertFalse(Path(str(self.state_path) + '.lock').exists())
        self.assertFalse((self.root / 'must-not-create').exists())
        return result

    def assert_success(self, result, shown, ignored=0):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'表示 {shown}件', result.stderr)
        self.assertIn(f'除外 {ignored}件', result.stderr)
        self.assertIn('エラー 0件', result.stderr)
        self.assertNotIn('プレビュー:', result.stdout)

    def test_file_uses_the_actual_post_formatter_without_config_or_state_access(self):
        source = self.source('受信サンプル.nmea', DCR_NMEA + '\n')
        result = self.run_cli('--preview', source, '--received-at', RECEIVED_AT.isoformat())
        self.assert_success(result, shown=1)
        self.assertIn(self.expected(DCR_NMEA), result.stdout)
        self.assertNotIn(self.expected(DCR_NMEA), result.stderr)

    def test_c10_evacuation_instruction_uses_the_live_formatter_without_side_effects(self):
        sentence = evacuation_report().nmea
        source = self.source('synthetic-evacuation-test.nmea', sentence + '\n')
        result = self.run_cli('--preview', source, '--received-at', RECEIVED_AT.isoformat(),
                              absent_state=True)
        self.assert_success(result, shown=1)
        expected = self.expected(sentence)
        self.assertIn(expected, result.stdout)
        self.assertIn('【訓練・試験】DCX／避難先の指定', result.stdout)
        self.assertIn('主範囲内の人への既定指示（IC-A-03）', result.stdout)
        self.assertNotIn(expected, result.stderr)

    def test_stdin_renders_each_report_including_duplicates_without_creating_state(self):
        data = ('\n'.join((DCR_NMEA, MARINE_NMEA, DCR_NMEA)) + '\n').encode()
        result = self.run_cli('--preview', '-', '--received-at', '2026-11-08T00:00:00Z',
                              data=data, absent_state=True)
        self.assert_success(result, shown=3)
        self.assertEqual(result.stdout.count(self.expected(DCR_NMEA)), 2)
        self.assertEqual(result.stdout.count(self.expected(MARINE_NMEA)), 1)
        self.assertLess(result.stdout.index(self.expected(DCR_NMEA)),
                        result.stdout.index(self.expected(MARINE_NMEA)))

    def test_multiple_files_and_explicit_timezone_keep_input_order(self):
        first = self.source('first.nmea', DCR_NMEA + '\n')
        second = self.source('second.nmea', MARINE_NMEA + '\n')
        result = self.run_cli('--preview', first, second, '--received-at', '2026-11-08T09:00:00+09:00')
        self.assert_success(result, shown=2)
        self.assertIn(self.expected(DCR_NMEA), result.stdout)
        self.assertIn(self.expected(MARINE_NMEA), result.stdout)
        self.assertLess(result.stdout.index(self.expected(DCR_NMEA)),
                        result.stdout.index(self.expected(MARINE_NMEA)))

    def test_binary_ubx_accepts_files_and_stdin(self):
        data = ubx_frame(DCR_NMEA) + ubx_frame(MARINE_NMEA)
        source = self.source('reports.ubx', data)
        for name, standard_input in ((str(source), None), ('-', data)):
            with self.subTest(source=name):
                result = self.run_cli('--preview', name, '--input-format', 'ubx',
                                      '--received-at', RECEIVED_AT.isoformat(), data=standard_input)
                self.assert_success(result, shown=2)
                for sentence in (DCR_NMEA, MARINE_NMEA):
                    report = azarashi.decode(ubx_frame(sentence), msg_type='ublox', timestamp=RECEIVED_AT)
                    self.assertIn(report_message(report), result.stdout)

    def test_bad_report_sets_failure_status_but_later_report_is_previewed(self):
        corrupt = DCR_NMEA[:-2] + '00'
        source = self.source('damaged.nmea', corrupt + '\n' + MARINE_NMEA + '\n')
        result = self.run_cli('--preview', source, '--received-at', RECEIVED_AT.isoformat())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(self.expected(MARINE_NMEA), result.stdout)
        self.assertNotIn(self.expected(DCR_NMEA), result.stdout)
        self.assertIn('表示 1件', result.stderr)
        self.assertIn('エラー 1件', result.stderr)

    def test_missing_file_does_not_prevent_processing_later_file(self):
        source = self.source('valid.nmea', DCR_NMEA + '\n')
        missing = self.root / 'missing.nmea'
        result = self.run_cli('--preview', missing, source, '--received-at', RECEIVED_AT.isoformat())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(str(missing), result.stderr)
        self.assertIn(self.expected(DCR_NMEA), result.stdout)
        self.assertIn('表示 1件', result.stderr)

    def test_null_and_unknown_are_excluded_like_live_delivery(self):
        data = (NULL_NMEA + '\n' + UNKNOWN_NMEA + '\n').encode()
        result = self.run_cli('--preview', '-', '--received-at', RECEIVED_AT.isoformat(), data=data)
        self.assert_success(result, shown=0, ignored=2)
        self.assertEqual(result.stdout, '')

    def test_empty_and_unrelated_input_do_not_claim_success(self):
        for data in (b'', b'\n$GPGGA,unrelated GPS traffic\n'):
            with self.subTest(data=data):
                result = self.run_cli('--preview', '-', data=data, absent_state=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertIn('表示 0件', result.stderr)

    def test_truncated_ubx_reports_failure_after_rendering_complete_frame(self):
        data = ubx_frame(DCR_NMEA) + ubx_frame(MARINE_NMEA)[:-3]
        result = self.run_cli('--preview', '-', '--input-format', 'ubx',
                              '--received-at', RECEIVED_AT.isoformat(), data=data)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(report_message(azarashi.decode(ubx_frame(DCR_NMEA), msg_type='ublox',
                                                     timestamp=RECEIVED_AT)), result.stdout)
        self.assertIn('表示 1件', result.stderr)

    def test_invalid_options_fail_before_opening_files_or_starting_the_daemon(self):
        for arguments in (
            ('--preview',), ('--preview', '-', '-'),
            ('--input-format', 'nmea'), ('--received-at', RECEIVED_AT.isoformat()),
            ('--preview', 'unused', '--input-format', 'gpsd'),
            ('--preview', 'unused', '--received-at', '2026-11-08T00:00:00'),
            ('--preview', 'unused', '--received-at', 'not-a-time'),
            ('--preview', 'unused', '--received-at', '0001-01-01T00:00:00+23:59'),
            ('--preview', 'unused', '--status'), ('--preview', 'unused', '--health'),
            ('--preview', 'unused', '--history'), ('--preview', 'unused', '--format', 'json'),
            ('--preview', 'unused', '--state', 'pending'), ('--preview', 'unused', '--limit', '1'),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('error:', result.stderr)


if __name__ == '__main__':
    unittest.main()
