from datetime import datetime, timezone
import io
import struct
import unittest
from unittest.mock import Mock

import azarashi
from azarashi.reports import dcx

from shirasu.preview import preview_stream


DCR = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
MARINE = '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B'
NULL = '$QZQSM,55,53B0000DE000000000000000000000000000000000000000000000000E41D10*04'
UNKNOWN = '$QZQSM,55,53B0000DFF00000000000000000000000000000000000000000000000B82B78*71'
RECEIVED_AT = datetime(2026, 11, 8, tzinfo=timezone.utc)


def ubx_frame(sentence, gnss=5, signal=1):
    data = bytes.fromhex(sentence.split(',')[2].split('*')[0] + '0')
    payload = bytes((gnss, 1, signal, 0, 8, 0, 2, 0))
    payload += b''.join(data[offset:offset + 4][::-1] for offset in range(0, 32, 4))
    message = b'\xb5\x62\x02\x13' + struct.pack('<H', len(payload)) + payload
    a = b = 0
    for byte in message[2:]:
        a = (a + byte) & 255
        b = (b + a) & 255
    return message + bytes((a, b))


def unrelated_ubx(payload):
    message = b'\xb5\x62\x0a\x04' + struct.pack('<H', len(payload)) + payload
    a = b = 0
    for byte in message[2:]:
        a = (a + byte) & 255
        b = (b + a) & 255
    return message + bytes((a, b))


class ShortReads(io.BytesIO):
    def read1(self, size=-1):
        return super().read(min(3, size))


class PreviewTests(unittest.TestCase):
    def run_preview(self, data, input_format='nmea', **kwargs):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.formatter = Mock(side_effect=lambda report: f'{report}\n---\nNMEA: {report.nmea}')
        options = dict(input_format=input_format, received_at=RECEIVED_AT,
                       message_formatter=self.formatter,
                       report_filter=lambda report: not isinstance(report, (dcx.NullMsg, dcx.Unknown)),
                       stdout=self.stdout, stderr=self.stderr, source_name='sample')
        options.update(kwargs)
        stream = io.BytesIO(data) if isinstance(data, bytes) else data
        counts = preview_stream(stream, **options)
        self.assertFalse(stream.closed)
        return counts

    def test_nmea_multi_report_and_duplicates_preserve_exact_message(self):
        counts = self.run_preview((DCR + '\n' + MARINE + '\n' + DCR).encode())
        self.assertEqual(counts, {'decoded': 3, 'shown': 3, 'ignored': 0, 'errors': 0})
        self.assertEqual(self.stderr.getvalue(), '')
        for call in self.formatter.call_args_list:
            report = call.args[0]
            self.assertEqual(report.timestamp, RECEIVED_AT)
            self.assertIn(f'{report}\n---\nNMEA: {report.nmea}', self.stdout.getvalue())
        self.assertEqual(self.stdout.getvalue().count('=== プレビュー'), 3)

    def test_normal_gps_traffic_and_noise_are_skipped(self):
        counts = self.run_preview(('noise\n$GPGGA,ordinary GPS data\nignored ' + DCR + '\n').encode())
        self.assertEqual(counts['shown'], 1)
        self.assertEqual(counts['errors'], 0)

    def test_concatenated_sentences_and_bad_nmea_recover(self):
        bad = DCR[:-2] + '00'
        counts = self.run_preview((bad + MARINE + DCR[:20] + DCR).encode())
        self.assertEqual(counts, {'decoded': 2, 'shown': 2, 'ignored': 0, 'errors': 2})
        self.assertEqual(len(self.stderr.getvalue().splitlines()), 2)

    def test_truncated_nmea_is_reported_and_terminates(self):
        counts = self.run_preview(DCR[:40].encode())
        self.assertEqual(counts['errors'], 1)
        self.assertEqual(counts['shown'], 0)

    def test_ubx_reads_complete_frames_across_short_read_boundaries(self):
        counts = self.run_preview(ShortReads(ubx_frame(DCR) + ubx_frame(MARINE)), 'ubx')
        self.assertEqual(counts, {'decoded': 2, 'shown': 2, 'ignored': 0, 'errors': 0})
        self.assertEqual(self.stderr.getvalue(), '')

    def test_ubx_checksum_failure_does_not_lose_following_frame(self):
        broken = ubx_frame(DCR)[:-1] + b'\x00'
        counts = self.run_preview(broken + ubx_frame(MARINE), 'ubx')
        self.assertEqual(counts, {'decoded': 1, 'shown': 1, 'ignored': 0, 'errors': 1})

    def test_ubx_corrupted_length_resynchronizes_at_eof(self):
        broken = bytearray(ubx_frame(DCR))
        broken[4:6] = struct.pack('<H', 1000)
        counts = self.run_preview(bytes(broken) + ubx_frame(MARINE), 'ubx')
        self.assertEqual(counts, {'decoded': 1, 'shown': 1, 'ignored': 0, 'errors': 1})
        self.assertIn('incomplete', self.stderr.getvalue())

    def test_ubx_invalid_length_does_not_block_following_frame(self):
        counts = self.run_preview(b'\xb5\x62\x02\x13\xff\xff' + ubx_frame(DCR), 'ubx')
        self.assertEqual(counts['shown'], 1)
        self.assertEqual(counts['errors'], 1)

    def test_ubx_partial_header_and_payload_are_errors(self):
        for fragment in (b'\xb5', b'\xb5\x62\x02', b'\xb5\x62\x02\x13\x28', ubx_frame(DCR)[:-2]):
            with self.subTest(fragment=fragment):
                counts = self.run_preview(fragment, 'ubx')
                self.assertEqual(counts['errors'], 1)
                self.assertEqual(counts['shown'], 0)

    def test_non_qzss_and_non_l1s_ubx_are_ignored(self):
        counts = self.run_preview(ubx_frame(DCR, gnss=0) + ubx_frame(DCR, signal=0) + ubx_frame(MARINE), 'ubx')
        self.assertEqual(counts, {'decoded': 1, 'shown': 1, 'ignored': 0, 'errors': 0})

    def test_unrelated_ubx_payload_cannot_be_mistaken_for_a_report(self):
        data = unrelated_ubx(b'info ' + ubx_frame(DCR) + b'\xb5') + ubx_frame(MARINE)
        counts = self.run_preview(data, 'ubx')
        self.assertEqual(counts, {'decoded': 1, 'shown': 1, 'ignored': 0, 'errors': 0})
        self.assertEqual(type(self.formatter.call_args.args[0]).__name__, 'Marine')

    def test_null_and_unknown_are_decoded_but_never_formatted(self):
        for kind, data in (('nmea', (NULL + '\n' + UNKNOWN + '\n').encode()),
                           ('ubx', ubx_frame(NULL) + ubx_frame(UNKNOWN))):
            with self.subTest(kind=kind):
                counts = self.run_preview(data, kind)
                self.assertEqual(counts, {'decoded': 2, 'shown': 0, 'ignored': 2, 'errors': 0})
                self.formatter.assert_not_called()
                self.assertEqual(self.stdout.getvalue(), '')

    def test_empty_and_non_report_files_have_no_errors(self):
        for kind, data in (('nmea', b''), ('ubx', b''), ('nmea', b'$GPGGA,test\n'), ('ubx', b'noise')):
            with self.subTest(kind=kind, data=data):
                self.assertEqual(self.run_preview(data, kind),
                                 {'decoded': 0, 'shown': 0, 'ignored': 0, 'errors': 0})

    def test_diagnostics_do_not_echo_raw_input_or_terminal_controls(self):
        self.run_preview((DCR[:30] + '\x1b[31m\n').encode(), source_name='file\x1b[31m\nname')
        diagnostic = self.stderr.getvalue()
        self.assertNotIn('\x1b', diagnostic)
        self.assertEqual(len(diagnostic.splitlines()), 1)
        self.assertNotIn(DCR[:30], diagnostic)

    def test_filter_and_formatter_failures_are_not_treated_as_bad_input(self):
        for override in ({'report_filter': Mock(side_effect=RuntimeError('filter failure'))},
                         {'message_formatter': Mock(side_effect=OSError('output failure'))}):
            with self.subTest(override=next(iter(override))):
                with self.assertRaises((RuntimeError, OSError)):
                    self.run_preview(DCR.encode(), **override)

    def test_read_failure_is_reported_once(self):
        reader = Mock(closed=False, read1=Mock(side_effect=OSError('read failure')))
        counts = self.run_preview(reader, 'ubx')
        self.assertEqual(counts['errors'], 1)
        reader.read1.assert_called_once()


if __name__ == '__main__':
    unittest.main()
