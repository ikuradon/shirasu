"""Renderer contract tests, also runnable outside the lightweight daemon image."""

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from shirasu.images.protocol import make_path
from image_service.app import app

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from image_service import render as renderer
    from azarashi.reports import dcr

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = json.loads((ROOT / 'image_service/examples/typhoon.json').read_text())
TYPHOON = tuple((row['nmea'], row['received_ms']) for row in SAMPLES)
MARINE = ('$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B', 1794121200000)
KEY = bytes(range(32))  # Public test fixture; never used by a deployment.


def change_fields(nmea, fields, *, valid_crc=True):
    """Construct protocol-valid synthetic fixtures with named bit-field offsets."""
    value = int(nmea.split(',')[2].split('*')[0] + '0', 16)
    for offset, length, replacement in fields:
        shift = 256 - offset - length
        value = (value & ~(((1 << length) - 1) << shift)) | (replacement << shift)
    if valid_crc:
        data = value.to_bytes(32, 'big')
        crc = 0
        for bit in range(226):
            source = (data[bit // 8] >> (7 - bit % 8)) & 1
            carry = ((crc >> 23) & 1) ^ source
            crc = (crc << 1) & 0xffffff
            if carry:
                crc ^= 0x864cfb
        value = (value & ~((2 ** 24 - 1) << 6)) | (crc << 6)
    body = '$QZQSM,55,' + f'{value:064X}'[:-1]
    checksum = 0
    for char in body[1:]:
        checksum ^= ord(char)
    return body + f'*{checksum:02X}'


def hypocenter():
    # A clearly marked synthetic training fixture: 35N/139E, M5, 10 km.
    fields = [(14, 3, 7), (17, 4, 2), (53, 161, 0), (80, 5, 9),
              (85, 5, 12), (90, 6, 0), (96, 9, 10), (105, 7, 50),
              (112, 10, 350), (123, 7, 35), (143, 8, 139)]
    return (change_fields(TYPHOON[0][0], fields), TYPHOON[0][1])


def request(path, method='GET', **environ):
    response = {}

    def start_response(status, headers):
        response['status'] = status
        response['headers'] = dict(headers)

    with patch.dict('os.environ', {'SHIRASU_IMAGE_KEYS': json.dumps({'test': KEY.hex()})}):
        response['body'] = b''.join(app({'REQUEST_METHOD': method, 'PATH_INFO': path,
                                       'QUERY_STRING': '', **environ}, start_response))
    return response


def leader_segments(artist):
    """Measure a drawn leader; support legacy arrows and explicit Line2D lines."""
    from matplotlib.path import Path as PlotPath
    arrow = getattr(artist, 'arrow_patch', None)
    drawn = arrow if arrow is not None else artist
    path = drawn.get_path().transformed(drawn.get_transform())
    previous = start = None
    result = []
    for vertices, code in path.iter_segments(curves=False, simplify=False):
        point = tuple(float(value) for value in vertices[-2:])
        if code == PlotPath.MOVETO:
            previous = start = point
        elif code == PlotPath.LINETO:
            if previous != point:
                result.append((previous, point))
            previous = point
        elif code == PlotPath.CLOSEPOLY:
            if previous != start:
                result.append((previous, start))
            previous = start
    return path, result


def segment_contacts(first, second):
    """Intersection points, or both ends of a collinear overlap, in pixels."""
    a, b = first
    c, d = second
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    cross = lambda u, v: u[0] * v[1] - u[1] * v[0]
    delta = (c[0] - a[0], c[1] - a[1])
    denominator = cross(r, s)
    epsilon = 1e-8
    if abs(denominator) > epsilon:
        t, u = cross(delta, s) / denominator, cross(delta, r) / denominator
        if -epsilon <= t <= 1 + epsilon and -epsilon <= u <= 1 + epsilon:
            return [(a[0] + t * r[0], a[1] + t * r[1])]
        return []
    if abs(cross(delta, r)) > epsilon:
        return []
    length_squared = r[0] ** 2 + r[1] ** 2
    if length_squared < epsilon:
        return []  # Zero-length pieces do not draw a visible leader.
    t0 = (delta[0] * r[0] + delta[1] * r[1]) / length_squared
    t1 = t0 + (s[0] * r[0] + s[1] * r[1]) / length_squared
    lower, upper = max(0, min(t0, t1)), min(1, max(t0, t1))
    if lower > upper + epsilon:
        return []
    return [(a[0] + t * r[0], a[1] + t * r[1]) for t in (lower, upper)]


class HTTPAuthenticationTests(unittest.TestCase):
    def test_invalid_signature_does_not_import_or_call_renderer(self):
        path = make_path(TYPHOON, 'test', KEY)
        path = path[:-5] + ('A' if path[-5] != 'A' else 'B') + path[-4:]
        with patch('builtins.__import__', wraps=__import__) as imports:
            response = request(path)
        self.assertEqual(response['status'], '403 Forbidden')
        self.assertEqual(response['headers']['Cache-Control'], 'no-store')
        self.assertNotIn('image_service.render', [call.args[0] for call in imports.call_args_list])
        self.assertNotIn(path.encode(), response['body'])

    def test_query_and_methods_are_rejected_before_rendering(self):
        path = make_path(TYPHOON, 'test', KEY)
        self.assertEqual(request(path, QUERY_STRING='color=red')['status'], '400 Bad Request')
        response = request(path, 'POST')
        self.assertEqual(response['status'], '405 Method Not Allowed')
        self.assertEqual(response['headers']['Allow'], 'GET, HEAD')

    def test_missing_key_configuration_is_unavailable(self):
        response = {}
        with patch.dict('os.environ', {'SHIRASU_IMAGE_KEYS': ''}):
            body = b''.join(app({'PATH_INFO': '/healthz'},
                               lambda status, headers: response.update(status=status, headers=dict(headers))))
        self.assertEqual(response['status'], '503 Service Unavailable')
        self.assertEqual(response['headers']['Cache-Control'], 'no-store')
        self.assertNotIn(b'traceback', body.lower())


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt to run the isolated PNG renderer tests')
class RendererTests(unittest.TestCase):
    def capture(self, records):
        figures = []
        original = renderer._figure

        def make_figure():
            figure = original()
            figures.append(figure)
            figure.clear = lambda *args, **kwargs: None
            return figure

        with patch.object(renderer, '_figure', side_effect=make_figure):
            image = renderer.render(records)
        figure = figures[0]
        self.addCleanup(lambda: renderer.Figure.clear(figure))
        return image, figure

    def assert_png(self, data):
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', data[16:24]), (1440, 1200))

    def assert_clear_leaders(self, figure, axes, expected_count):
        figure.canvas.draw()
        artists = [artist for artist in axes.artists
                   if hasattr(artist, 'offsetbox') and hasattr(artist, 'xy')]
        self.assertEqual(len(artists), expected_count, 'Every source point must retain its label')
        drawn_leaders = [line for line in axes.lines if line.get_gid() == 'label-leader']
        if not drawn_leaders:
            drawn_leaders = [artist for artist in artists if artist.arrow_patch is not None]
        self.assertEqual(len(drawn_leaders), expected_count, 'Every label must retain its own leader')
        boxes = [artist.offsetbox.get_window_extent(figure.canvas.get_renderer()) for artist in artists]
        targets = [axes.transData.transform(artist.xy) for artist in artists]
        leaders = [leader_segments(artist) for artist in drawn_leaders]
        problems = []
        for index, box in enumerate(boxes):
            for other in range(index + 1, len(boxes)):
                if box.overlaps(boxes[other]):
                    problems.append(f'labels {index} and {other} overlap')
        for index, (path, segments) in enumerate(leaders):
            self.assertTrue(segments, f'Leader {index} must remain visible')
            for other, box in enumerate(boxes):
                if other != index and path.intersects_bbox(box, filled=False):
                    problems.append(f'leader {index} crosses label {other}')
            for other in range(index + 1, len(leaders)):
                common_root = math.dist(targets[index], targets[other]) <= 1e-6
                for first in segments:
                    for second in leaders[other][1]:
                        contacts = segment_contacts(first, second)
                        # Coincident forecast centres may share only the small
                        # root hidden by their marker, not an outbound segment.
                        if contacts and not (common_root and all(
                                math.dist(point, targets[index]) <= 12 for point in contacts)):
                            problems.append(f'leaders {index} and {other} cross or overlap')
        self.assertEqual(sorted(set(problems)), [], '; '.join(sorted(set(problems))))

    def test_real_typhoon_leaders_do_not_cross_labels_or_each_other(self):
        _, figure = self.capture(TYPHOON)
        self.assertEqual(len(figure.axes), 1, 'The six-point sample must retain its map')
        self.assert_clear_leaders(figure, figure.axes[0], 6)

    def test_bent_dense_and_coincident_tracks_have_clear_rendered_leaders(self):
        layouts = {
            'bent': [(0.25, 0.35), (0.42, 0.57), (0.58, 0.35),
                     (0.74, 0.57), (0.60, 0.67), (0.42, 0.66)],
            'dense': [(0.43, 0.40), (0.46, 0.43), (0.49, 0.40),
                      (0.52, 0.43), (0.55, 0.40), (0.58, 0.43)],
            'coincident': [(0.5, 0.4)] * 6,
        }
        labels = [[(f'11/{10 + index} 21:00', 19, renderer.WHITE, True),
                   ('980 hPa', 22, renderer.MUTED, False)] for index in range(6)]
        for name, points in layouts.items():
            with self.subTest(layout=name):
                figure = renderer._figure()
                try:
                    axes = figure.add_axes([0, 0, 1, 1])
                    axes.set_xlim(0, 1)
                    axes.set_ylim(0, 1)
                    renderer._annotations(figure, axes, points, labels)
                    self.assert_clear_leaders(figure, axes, 6)
                finally:
                    figure.clear()

    def test_real_typhoon_bundle_is_deterministic_and_has_every_point(self):
        first, figure = self.capture(TYPHOON)
        self.assert_png(first.png)
        self.assertEqual(first.category, 'Typhoon')
        self.assertEqual(first.png, renderer.render(TYPHOON).png)
        self.assertEqual(len(figure.axes[0].collections), 6)
        self.assertEqual(len(figure.axes[0].artists), 6)
        # Inspect actual rendered annotation boxes, not just the placement model.
        boxes = [artist.offsetbox.get_window_extent(figure.canvas.get_renderer())
                 for artist in figure.axes[0].artists]
        for index, box in enumerate(boxes):
            self.assertGreaterEqual(box.x0, 0)
            self.assertLessEqual(box.x1, renderer.WIDTH)
            for other in boxes[index + 1:]:
                self.assertFalse(box.overlaps(other))

    def test_second_category_uses_epicentre_only(self):
        image, figure = self.capture([hypocenter()])
        self.assert_png(image.png)
        self.assertEqual(image.category, 'Hypocenter')
        self.assertEqual(len(figure.axes), 1)
        self.assertEqual(len(figure.axes[0].collections), 1)
        self.assertTrue(any('訓練・試験' in text.get_text() for text in figure.texts))
        self.assertTrue(all(isinstance(patch, renderer.Polygon) for patch in figure.axes[0].patches))

    def test_unmapped_category_has_summary_without_invented_geography(self):
        image, figure = self.capture([MARINE])
        self.assert_png(image.png)
        self.assertEqual(image.category, 'Marine')
        self.assertEqual(figure.axes, [])
        self.assertTrue(any('海上' in text.get_text() for text in figure.texts))

    def test_hypocenter_retains_received_tsunami_notice(self):
        sentence, received = hypocenter()
        warning = change_fields(sentence, [(53, 9, 211)])
        _, figure = self.capture([(warning, received)])
        from matplotlib.text import Text
        rendered_text = ''.join(item.get_text() for item in figure.findobj(match=Text))
        self.assertIn('津波警報', rendered_text)

    def test_cancellation_is_prominent_and_does_not_draw_forecast(self):
        cancelled = change_fields(TYPHOON[0][0], [(41, 2, 2), (14, 3, 7)])
        _, figure = self.capture([(cancelled, TYPHOON[0][1])])
        text = '\n'.join(item.get_text() for item in figure.texts)
        self.assertIn('【訓練・試験】【取消】', text)
        self.assertIn('取り消されました', text)
        self.assertEqual(figure.axes, [])

    def test_unknown_coordinates_use_text_summary(self):
        undefined = change_fields(TYPHOON[0][0], [(103, 7, 127)])
        _, figure = self.capture([(undefined, TYPHOON[0][1])])
        self.assertEqual(figure.axes, [])
        self.assertTrue(any('未定義' in item.get_text() for item in figure.texts))

    def test_mixed_categories_and_conflicting_forecasts_are_rejected(self):
        with self.assertRaises(ValueError):
            renderer.render([TYPHOON[0], MARINE])
        changed = change_fields(TYPHOON[0][0], [(143, 11, 950)])
        with self.assertRaises(ValueError):
            renderer.render([TYPHOON[0], (changed, TYPHOON[0][1] + 1)])

    def test_month_year_rollover_uses_receipt_context(self):
        receipt = int(datetime(2026, 12, 31, 12, 55, tzinfo=timezone.utc).timestamp() * 1000)
        # report 12/31 12:50 UTC, forecast 1/1 12:00 UTC
        sentence = change_fields(TYPHOON[1][0], [(21, 4, 12), (25, 5, 31),
                                                (53, 5, 1), (58, 5, 12), (63, 6, 0)])
        _, figure = self.capture([(sentence, receipt)])
        report = renderer._decode([(sentence, receipt)])[0]
        self.assertEqual(report.reference_time.year, 2027)
        self.assertEqual(renderer._time(report.reference_time, full=True), '2027/01/01 21:00')

    def test_coincident_positions_still_have_separate_labels(self):
        rectangles = renderer.place_labels([(0.5, 0.4)] * 6, [(0.18, 0.09)] * 6)
        self.assertEqual(len(rectangles), 6)
        for index, rect in enumerate(rectangles):
            for other in rectangles[index + 1:]:
                self.assertEqual(renderer._overlap(rect, other), 0)

    def test_dateline_projection_keeps_nearby_centres_close(self):
        figure = renderer._figure()
        try:
            axes, points = renderer._map(figure, [(179, 25), (-179, 26)])
            self.assertLess(abs(points[1][0] - points[0][0]), 3)
            self.assertLess(axes.get_xlim()[1] - axes.get_xlim()[0], 30)
        finally:
            figure.clear()

    def test_decoder_global_multipart_state_is_cleared_on_error(self):
        def fail(*args, **kwargs):
            dcr.NankaiTroughEarthquake.reports = {'old': 'unrelated request'}
            raise ValueError('bad data')
        with patch.object(renderer, '_decode', side_effect=fail):
            with self.assertRaises(ValueError):
                renderer.render(TYPHOON)
        self.assertEqual(dcr.NankaiTroughEarthquake.reports, {})
        self.assertIsNone(dcr.NankaiTroughEarthquake.announcement)

    def test_success_cache_etag_head_and_health_contract(self):
        path = make_path([MARINE], 'test', KEY)
        get = request(path)
        self.assertEqual(get['status'], '200 OK')
        self.assert_png(get['body'])
        self.assertIn('immutable', get['headers']['Cache-Control'])
        self.assertEqual(get['headers']['Vercel-CDN-Cache-Control'], 'public, max-age=31536000')
        self.assertEqual(get['headers']['ETag'], '"' + hashlib.sha256(get['body']).hexdigest() + '"')
        self.assertNotIn('Set-Cookie', get['headers'])
        head = request(path, 'HEAD')
        self.assertEqual(head['headers'], get['headers'])
        self.assertEqual(head['body'], b'')
        unchanged = request(path, HTTP_IF_NONE_MATCH=get['headers']['ETag'])
        self.assertEqual(unchanged['status'], '304 Not Modified')
        self.assertEqual(unchanged['body'], b'')
        self.assertEqual(request('/healthz')['headers']['Cache-Control'], 'no-store')

    def test_signed_bad_crc_is_bad_request_and_not_cached(self):
        bad = change_fields(TYPHOON[0][0], [(143, 11, 950)], valid_crc=False)
        path = make_path([(bad, TYPHOON[0][1])], 'test', KEY)
        response = request(path)
        self.assertEqual(response['status'], '400 Bad Request')
        self.assertEqual(response['headers']['Cache-Control'], 'no-store')
        self.assertNotIn(bad.encode(), response['body'])


if __name__ == '__main__':
    unittest.main()
