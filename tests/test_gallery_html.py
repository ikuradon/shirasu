"""The offline review document preserves provenance and contains only local assets."""

import copy
from html.parser import HTMLParser
import unittest

from scripts.gallery_html import render_html


def sample():
    return {
        'schema_version': 1, 'published_version': 'v4', 'cases': [{
            'slug': 'ash-received', 'title': '降灰予報 / 6時間分', 'category': 'AshFall',
            'source_kind': 'receiver', 'classification': 'training', 'state': 'issue',
            'description': '3市町村の6時間分を比較します。', 'source_label': '公開受信ログ',
            'source_url': 'https://github.com/example/sample?view=one&version=two',
            'source_file': 'tests/fixtures/qzqsm/recording.log', 'source_sha256': 'a' * 64,
            'tags': ['複数電文'], 'records_path': 'records/ash-received.json',
            'record_count': 5, 'reference_times': ['2026-09-24T00:57:00+00:00'],
            'report_text': '訓練です。\n阿蘇山の降灰予報。',
            'images': {
                'published': {'path': 'images/ash-received-v4.png', 'version': 'v4',
                              'kind': 'summary', 'sha256': 'b' * 64, 'width': 1440, 'height': 1200},
                'preview': {'path': 'images/ash-received-preview.png', 'version': 'preview',
                            'kind': 'map', 'sha256': 'c' * 64, 'width': 1440, 'height': 1200},
            },
        }],
    }


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        self.elements.append((tag, dict(attributes)))

    def attrs(self, tag):
        return [attrs for actual, attrs in self.elements if actual == tag]


class GalleryHtmlTests(unittest.TestCase):
    def test_document_exposes_provenance_separate_from_training_and_side_by_side_images(self):
        content = render_html(sample())
        document = Document(content)
        self.assertEqual(document.attrs('html'), [{'lang': 'ja'}])
        self.assertEqual([image['src'] for image in document.attrs('img')],
                         ['images/ash-received-v4.png', 'images/ash-received-preview.png'])
        for image in document.attrs('img'):
            self.assertEqual((image['width'], image['height']), ('1440', '1200'))
            self.assertEqual(image['loading'], 'lazy')
            self.assertIn('降灰予報', image['alt'])
        article, = document.attrs('article')
        self.assertEqual(article['data-source-kind'], 'receiver')
        self.assertEqual(article['data-classification'], 'training')
        self.assertEqual(article['data-state'], 'issue')
        self.assertIn('実受信', content)
        self.assertIn('訓練・試験', content)
        self.assertIn('要旨', content)
        self.assertIn('地図', content)
        self.assertIn('現在の災害情報ではありません', content)
        self.assertIn('基準時刻', content)
        self.assertIn('2026-09-24T00:57:00+00:00', content)
        self.assertTrue(any(link['href'] == 'records/ash-received.json'
                            and link.get('target') == '_blank'
                            and 'download' not in link for link in document.attrs('a')))

    def test_filters_have_labels_and_all_assets_work_without_external_fetches(self):
        content = render_html(sample())
        document = Document(content)
        self.assertEqual({select['id'] for select in document.attrs('select')},
                         {'category', 'source-kind', 'classification', 'state'})
        self.assertEqual(document.attrs('script'), [{}])
        self.assertEqual(document.attrs('link'), [])
        self.assertNotIn('fetch(', content)
        self.assertNotIn('XMLHttpRequest', content)
        self.assertEqual(len(document.attrs('noscript')), 1)
        self.assertTrue(any(item.get('aria-live') == 'polite' for item in document.attrs('p')))
        self.assertTrue(any(item.get('id') == 'empty' for item in document.attrs('section')))
        self.assertIn('@media(max-width:680px)', content)

    def test_topic_card_is_distinct_from_published_summary_and_retains_original_text(self):
        manifest = sample()
        case = manifest['cases'][0]
        case['images']['preview']['kind'] = 'topic'
        content = render_html(manifest)
        images = Document(content).attrs('img')
        self.assertTrue(images[0]['alt'].endswith('公開版 / 要旨'))
        self.assertTrue(images[1]['alt'].endswith('作業版 / トピック'))
        self.assertIn('<span class="kind">トピック</span>', content)
        self.assertIn('<span class="kind">要旨</span>', content)
        self.assertIn(case['report_text'], content)
        self.assertIn(case['records_path'], content)

    def test_source_text_and_error_messages_cannot_inject_markup_or_javascript(self):
        manifest = sample()
        case = manifest['cases'][0]
        attack = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'
        for key in ('title', 'description', 'source_label', 'source_file', 'source_sha256', 'report_text'):
            case[key] = attack
        case['source_url'] = 'javascript:alert(1)'
        case['tags'] = [attack]
        case['images']['preview'] = {'version': 'preview', 'error': attack}
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual(len(document.attrs('script')), 1)
        self.assertEqual(len(document.attrs('img')), 1)
        self.assertNotIn(attack, content)
        self.assertNotIn('href="javascript:', content)
        self.assertIn('&lt;/script&gt;', content)
        self.assertIn('描画できませんでした', content)
        for _, attrs in document.elements:
            self.assertNotIn('onerror', attrs)

    def test_generated_asset_links_cannot_escape_the_output_or_fetch_remote_images(self):
        paths = ('../sample.png', '/tmp/sample.png', 'https://example.com/sample.png',
                 '//example.com/sample.png', 'images/%2e%2e/sample.png',
                 'images/./sample.png', 'images\\sample.png', 'images//sample.png')
        for path in paths:
            for destination in ('records', 'image'):
                with self.subTest(path=path, destination=destination):
                    manifest = sample()
                    if destination == 'records':
                        manifest['cases'][0]['records_path'] = path
                    else:
                        manifest['cases'][0]['images']['preview']['path'] = path
                    with self.assertRaises(ValueError):
                        render_html(manifest)

    def test_duplicate_or_invalid_ids_cannot_break_links_and_filters(self):
        manifest = sample()
        manifest['cases'].append(copy.deepcopy(manifest['cases'][0]))
        with self.assertRaises(ValueError):
            render_html(manifest)
        for slug in ('../case', 'a" onclick="alert(1)', '', 'CASE'):
            manifest = sample()
            manifest['cases'][0]['slug'] = slug
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                render_html(manifest)


if __name__ == '__main__':
    unittest.main()
