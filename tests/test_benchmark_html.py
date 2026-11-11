"""Offline measurement report keeps comparisons paired and evidence explicit."""

from copy import deepcopy
from html.parser import HTMLParser
import unittest

from scripts.benchmark_html import render_html


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def attrs(self, tag):
        return [attrs for kind, attrs in self.elements if kind == tag]


def image(name, size, exact, *, encode=12.5):
    extension = 'png' if name.startswith('png-') else 'webp'
    return {
        'id': name, 'path': f'images/sample/{name}.{extension}',
        'mime': 'image/' + extension, 'bytes': size, 'width': 1920, 'height': 1080,
        'parameters': {'quality': 90},
        'encode_ms': {'samples': [encode], 'median': encode} if encode is not None else None,
        'decode_ms': {'samples': [4.25], 'median': 4.25},
        'pixel_exact': exact, 'psnr_rgb_db': None if exact else 42.4,
        'alpha_flattened': False, 'warnings': [],
    }


def sample():
    return {'schema_version': 1, 'kind': 'render-benchmark',
            'settings': {'warm_repeats': 3, 'encoding_repeats': 3, 'renderer': 'preview'},
            'environment': {'python': '3.x', 'machine': 'aarch64'},
            'results': [{
                'id': 'sample', 'title': '気象警報・注意報', 'category': 'Weather',
                'usages': [{'case_slug': 'weather', 'role': 'case'},
                           {'case_slug': 'weather', 'role': 'post', 'post_number': 1}],
                'record_count': 2, 'records_path': 'records/sample.json',
                'cold': {'import_ms': 800, 'render_ms': 500, 'process_ms': 9999},
                'warm': {'render_ms': {'samples': [120, 130, 140], 'median': 130,
                                       'min': 120, 'max': 140}},
                'peak_rss_mib': 155.1, 'stable_png': True,
                'formats': [image('png-original', 102400, True, encode=None),
                            image('webp-lossless', 51200, True),
                            image('webp-q90', 20480, False)],
            }]}


class BenchmarkHtmlTests(unittest.TestCase):
    def test_report_keeps_timing_scopes_sizes_and_comparison_inputs(self):
        content = render_html(sample())
        self.assertIn('800.0 ms', content)
        self.assertIn('500.0 ms', content)
        self.assertIn('130.0 ms', content)
        self.assertNotIn('9,999', content)
        self.assertIn('100.0 KiB', content)
        self.assertIn('50.0%', content)
        self.assertIn('0.10 MiB', content)
        self.assertIn('PNG生成後の追加変換', content)
        self.assertIn('OSのディスクキャッシュは消去していません', content)
        self.assertIn('RGB PSNR 42.40 dB', content)
        self.assertIn('RGBA画素が元PNGと完全一致', content)
        self.assertIn('weather / 投稿1', content)
        document = Document(content)
        self.assertEqual([row['src'] for row in document.attrs('img')],
                         [row['path'] for row in sample()['results'][0]['formats']])
        selected = [row['value'] for row in document.attrs('option') if 'selected' in row]
        self.assertEqual(selected, ['sample', 'webp-lossless', '360'])
        self.assertEqual(len(document.attrs('script')), 1)
        self.assertTrue(all('src' not in attrs for attrs in document.attrs('script')))

    def test_aggregate_compares_only_matching_successful_images(self):
        manifest = sample()
        second = deepcopy(manifest['results'][0])
        second.update(id='other', title='別画像')
        second['formats'] = [image('png-original', 1024000, True, encode=None),
                             {'id': 'webp-lossless', 'error': 'codec unavailable', 'path': None}]
        manifest['results'].append(second)
        content = render_html(manifest)
        self.assertIn('50.0%', content)  # 51200 / 102400, not all PNGs.
        self.assertNotIn('4.5%', content)
        self.assertIn('codec unavailable', content)
        self.assertIn('形式変換失敗 1件', content)
        self.assertEqual(len(Document(content).attrs('img')), 4)

    def test_slowest_first_table_jump_link_and_png_compression_detail(self):
        manifest = sample()
        slower = deepcopy(manifest['results'][0])
        slower.update(id='slower', title='遅い画像')
        slower['warm']['render_ms']['median'] = 400
        slower['warm']['png_save_ms'] = {'median': 32.1, 'samples': [32.1]}
        manifest['results'].append(slower)
        manifest['results'].append({'id': 'failed', 'title': '失敗', 'error': 'failed'})
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual([attrs['data-show-image'] for attrs in document.attrs('a')
                          if 'data-show-image' in attrs], ['slower', 'sample', 'failed'])
        self.assertIn('再描画の中央値が長い順', content)
        self.assertIn('うちPNG圧縮 中央値 32.1 ms', content)
        self.assertIn('#comparison', [attrs.get('href') for attrs in document.attrs('a')])
        manifest['codec_error_count'] = 7
        self.assertIn('形式変換失敗 7件', render_html(manifest))

    def test_escape_content_attributes_and_report_transparency(self):
        manifest = sample()
        row = manifest['results'][0]
        row['title'] = '<script>alert("title")</script>'
        row['formats'][1]['parameters'] = {'x': '</pre><script>alert(1)</script>'}
        row['formats'][1]['warnings'] = ['<img src=x onerror=alert(1)>']
        row['formats'][1]['alpha_flattened'] = True
        manifest['environment']['unsafe'] = '</pre><script>alert(1)</script>'
        content = render_html(manifest)
        self.assertNotIn('<script>alert', content)
        self.assertIn('&lt;script&gt;', content)
        self.assertIn('透明部分を白背景へ合成', content)
        self.assertEqual(len(Document(content).attrs('img')), 3)
        self.assertTrue(all('onerror' not in attrs for attrs in Document(content).attrs('img')))

    def test_unsafe_paths_and_invalid_image_dimensions_are_rejected(self):
        for path in ('../sample.png', '/sample.png', 'https://example.test/a.png', 'images/a".png'):
            manifest = sample()
            manifest['results'][0]['formats'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                render_html(manifest)
        manifest = sample()
        manifest['results'][0]['formats'][0]['width'] = True
        with self.assertRaises(ValueError):
            render_html(manifest)

    def test_render_failure_and_missing_metrics_remain_visible_without_nan(self):
        manifest = sample()
        manifest['results'].append({'id': 'failed', 'title': '失敗', 'error': '描画失敗 <sample>',
                                    'formats': []})
        row = manifest['results'][0]
        row['warm']['render_ms']['median'] = float('nan')
        row['formats'][2]['psnr_rgb_db'] = float('inf')
        content = render_html(manifest)
        self.assertIn('描画失敗 &lt;sample&gt;', content)
        self.assertIn('描画失敗 1件', content)
        self.assertNotIn('nan', content.lower())
        self.assertNotIn('inf dB', content)
        self.assertIn('<noscript>', content)
        self.assertEqual(len([attrs for tag, attrs in Document(content).elements
                              if tag == 'article']), 2)


if __name__ == '__main__':
    unittest.main()
