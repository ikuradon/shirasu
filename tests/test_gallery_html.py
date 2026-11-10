"""The offline review document preserves provenance and contains only local assets."""

import copy
from html.parser import HTMLParser
import json
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
                         {'category', 'source-kind', 'classification', 'state', 'reason', 'image-width'})
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

    def test_preview_only_manifest_uses_same_cards_without_a_missing_published_image(self):
        manifest = sample()
        del manifest['published_version']
        manifest.update(renderer='preview', commit='abc123', working_tree=True)
        case = manifest['cases'][0]
        del case['images']['published']
        case['images']['preview']['decision'] = {'mode': 'map', 'reasons': []}
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual([image['src'] for image in document.attrs('img')],
                         ['images/ash-received-preview.png'])
        self.assertIn('class="comparison-grid single"', content)
        self.assertIn('作業版の画像と、受信内容に応じた表示理由', content)
        self.assertNotIn('公開済み版', content)
        self.assertIn('<code>abc123</code>', content)
        self.assertIn('未コミットの変更を含む', content)
        self.assertIn(case['report_text'], content)
        self.assertIn(case['source_sha256'], content)
        self.assertIn(case['records_path'], content)

    def test_old_manifest_does_not_infer_reasons_from_summary_or_map_kind(self):
        content = render_html(sample())
        document = Document(content)
        self.assertEqual(content.count('表示理由：記録なし'), 2)
        self.assertNotIn('切替なし（対応した表示）', content)
        self.assertEqual(json.loads(document.attrs('article')[0]['data-reason-codes']),
                         ['__unrecorded__'])
        self.assertIn('data-reason-count="__unrecorded__">1</strong>', content)

    def test_recorded_empty_reasons_are_distinct_from_unrecorded_published_image(self):
        manifest = sample()
        manifest['cases'][0]['images']['preview']['decision'] = {'mode': 'area', 'reasons': []}
        content = render_html(manifest)
        document = Document(content)
        self.assertIn('表示方法：対象区域', content)
        self.assertIn('切替なし（対応した表示）', content)
        self.assertEqual(content.count('表示理由：記録なし'), 1)
        self.assertEqual(json.loads(document.attrs('article')[0]['data-reason-codes']),
                         ['__unchanged__'])
        # Only preview decisions contribute to filters, even when comparing with an old image.
        self.assertNotIn('__unrecorded__', {option['value'] for option in document.attrs('option')})

    def test_reason_counts_use_stable_codes_and_count_each_case_once_per_reason(self):
        manifest = sample()
        reasons = [
            {'code': 'no_geometry', 'stage': 'geometry', 'message': '有効な地点・範囲の指定なし',
             'detail': 'A12〜A16=0'},
            {'code': 'missing_boundary', 'stage': 'area', 'message': '対象区域の境界データが未収録',
             'detail': '区域コード: 1234'},
        ]
        first = manifest['cases'][0]
        first['images']['preview']['decision'] = {'mode': 'topic', 'reasons': reasons + reasons[:1]}
        second = copy.deepcopy(first)
        second['slug'] = 'second'
        second['images']['preview']['decision'] = {'mode': 'area', 'reasons': reasons[:1]}
        second['images']['published']['decision'] = {
            'mode': 'summary', 'reasons': [{'code': 'published_only', 'stage': 'render',
                                         'message': '公開版の理由', 'detail': ''}]}
        manifest['cases'].append(second)
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual(json.loads(document.attrs('article')[0]['data-reason-codes']),
                         ['no_geometry', 'missing_boundary'])
        self.assertIn('data-reason-count="no_geometry">2</strong>', content)
        self.assertIn('data-reason-count="missing_boundary">1</strong>', content)
        option_values = {item['value'] for item in document.attrs('option')}
        self.assertIn('no_geometry', option_values)
        self.assertIn('missing_boundary', option_values)
        self.assertNotIn('published_only', option_values)
        self.assertIn('表示方法：トピック', content)
        self.assertIn('class="reason-stage">地点・範囲</span>', content)
        self.assertIn('class="reason-stage">対象区域</span>', content)
        self.assertIn('A12〜A16=0', content)
        self.assertIn('区域コード: 1234', content)

    def test_render_errors_remain_errors_even_if_a_decision_is_attached(self):
        manifest = sample()
        manifest['cases'][0]['images']['preview'] = {
            'error': 'PNG writer failed',
            'decision': {'mode': 'topic', 'reasons': [
                {'code': 'no_geometry', 'stage': 'geometry', 'message': '正常な表示切替', 'detail': ''}]},
        }
        content = render_html(manifest)
        document = Document(content)
        self.assertIn('描画できませんでした', content)
        self.assertIn('PNG writer failed', content)
        self.assertNotIn('正常な表示切替', content)
        self.assertNotIn('no_geometry', content)
        self.assertEqual(json.loads(document.attrs('article')[0]['data-reason-codes']), ['__error__'])
        self.assertIn('data-reason-count="__error__">1</strong>', content)

    def test_reason_code_stage_message_detail_and_mode_cannot_inject_markup(self):
        manifest = sample()
        attack = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'
        manifest['cases'][0]['images']['preview']['decision'] = {
            'mode': attack, 'reasons': [dict.fromkeys(('code', 'stage', 'message', 'detail'), attack)],
        }
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual(len(document.attrs('script')), 1)
        self.assertEqual(len(document.attrs('img')), 2)
        self.assertNotIn(attack, content)
        self.assertIn('&lt;/script&gt;', content)
        self.assertEqual(json.loads(document.attrs('article')[0]['data-reason-codes']), [attack])
        for _, attributes in document.elements:
            self.assertNotIn('onerror', attributes)

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
