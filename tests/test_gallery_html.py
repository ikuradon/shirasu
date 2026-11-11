"""The offline review document preserves provenance and contains only local assets."""

import copy
from html.parser import HTMLParser
import json
import unittest

from image_publication import with_image
from scripts.gallery_html import _post_content, render_html


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


def posting_sample(*, multiple=False):
    manifest = sample()
    case = manifest['cases'][0]
    case['posts'] = []
    for index in range(5 if multiple else 1):
        case['posts'].append({
            'kind': 'single', 'text': f'【訓練・試験】降灰予報 {index + 1}\n本文をそのまま表示。'
            f'\n---\nNMEA: $QZQSM,55,{index}*AB', 'record_indexes': [index],
            'record_count': 1, 'image_enabled': True,
            'images': {side: {**image, 'path': f'images/post-{index + 1}-{side}.png'}
                       for side, image in case['images'].items()},
        })
    return manifest


def baseline_sample(*, multiple=False):
    manifest = posting_sample(multiple=multiple)
    case = manifest['cases'][0]
    previous = copy.deepcopy(case)
    previous['records_path'] = 'baseline/records/ash-received.json'
    previous['images']['preview']['path'] = 'baseline/images/ash-received-preview.png'
    for index, post in enumerate(previous['posts'], 1):
        post['images']['preview']['path'] = f'baseline/images/post-{index}-preview.png'
    case['comparison'] = {
        'images': {'status': 'changed', 'changes': ['image', 'mode', 'reasons'],
                   'note': '同じ入力の画像と表示理由が変わりました。'},
        'posts': {'status': 'unchanged', 'changes': [], 'note': '投稿は同じです。'},
        'previous': previous,
    }
    manifest.update(commit='current123', working_tree=True, baseline={
        'commit': 'previous456', 'working_tree': False, 'manifest_path': 'baseline/manifest.json',
        'counts': {'images': {'changed': 1}, 'posts': {'unchanged': 1}},
        'unmatched_cases': [{'slug': 'other-previous', 'title': '今回選択していない前回の例'}],
    })
    return manifest


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        self.elements.append((tag, dict(attributes)))

    def attrs(self, tag):
        return [attrs for actual, attrs in self.elements if actual == tag]


class PostContents(HTMLParser):
    """Read ordinary post text, replacing each local media node with its URL slot."""

    MEDIA = '[local PNG]'

    def __init__(self, source):
        super().__init__()
        self.bodies = []
        self.parts = []
        self.body_depth = 0
        self.media_depth = 0
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        classes = dict(attributes).get('class', '').split()
        if not self.body_depth:
            if 'post-body' in classes:
                self.body_depth = 1
                self.parts = []
        elif self.media_depth:
            if tag != 'img':
                self.media_depth += 1
        elif {'post-attachment', 'post-attachment-error'}.intersection(classes):
            self.parts.append(self.MEDIA)
            self.media_depth = 1
        elif tag != 'img':
            self.body_depth += 1

    def handle_endtag(self, tag):
        if self.media_depth:
            self.media_depth -= 1
        elif self.body_depth:
            self.body_depth -= 1
            if not self.body_depth:
                self.bodies.append(''.join(self.parts))

    def handle_data(self, data):
        if self.body_depth and not self.media_depth:
            self.parts.append(data)


class GalleryHtmlTests(unittest.TestCase):
    def test_optional_post_is_escaped_and_separate_from_the_decoded_source_text(self):
        manifest = sample()
        case = manifest['cases'][0]
        attack = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'
        case['post_text'] = '訓練の既定指示\n' + attack + '\n---\nNMEA: $QZQSM,55,0123*AB'
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual(len(document.attrs('script')), 1)
        self.assertEqual(len(document.attrs('img')), 2)
        self.assertEqual(content.count('<summary>投稿本文</summary>'), 1)
        self.assertIn('<details class="source-details post-details">', content)
        self.assertIn('訓練の既定指示\n&lt;/script&gt;&lt;script&gt;alert(&quot;x&quot;)', content)
        self.assertIn('\n---\nNMEA: $QZQSM,55,0123*AB</pre>', content)
        self.assertNotIn(attack, content)
        self.assertIn('</details>\n  <details class="source-details">'
                      '<summary>出典・入力データを確認</summary>', content)
        self.assertIn('<h4>電文のテキスト</h4><pre>' + case['report_text'] + '</pre>', content)
        for _, attributes in document.elements:
            self.assertNotIn('onerror', attributes)

    def test_absent_or_empty_post_keeps_the_existing_document_without_an_extra_section(self):
        manifest = sample()
        original = render_html(manifest)
        self.assertNotIn('post-details', original)
        self.assertNotIn('<summary>投稿本文</summary>', original)
        for value in (None, ''):
            with self.subTest(post_text=value):
                manifest['cases'][0]['post_text'] = value
                self.assertEqual(render_html(manifest), original)

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
                         {'category', 'source-kind', 'classification', 'state', 'reason', 'image-width', 'view-mode'})
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

    def test_new_posts_default_to_whole_post_and_legacy_manifest_defaults_to_images(self):
        legacy = Document(render_html(sample()))
        self.assertEqual(legacy.attrs('body'), [{'data-view-mode': 'images'}])
        legacy_option, = [option for option in legacy.attrs('option') if option.get('value') == 'posts']
        self.assertIn('disabled', legacy_option)
        manifest = posting_sample()
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual(document.attrs('body'), [{'data-view-mode': 'posts'}])
        option, = [option for option in document.attrs('option') if option.get('value') == 'posts']
        self.assertIn('selected', option)
        self.assertNotIn('disabled', option)
        self.assertIn('画像有効・履歴なしで、入力が待機時間内に揃った場合', content)
        self.assertIn('添付画像はローカルPNGで表示', content)
        self.assertIn('<p class="intro">投稿本文と添付画像を、投稿単位で確認できます。', content)
        self.assertIn('「画像のみ」に切り替えると画像と表示理由を確認できます。', content)
        self.assertEqual(len(document.attrs('script')), 1)
        self.assertNotIn('fetch(', content)
        self.assertNotIn('XMLHttpRequest', content)
        self.assertEqual(document.attrs('link'), [])

    def test_reference_background_note_explains_why_the_post_attachment_can_differ(self):
        manifest = posting_sample()
        note = ('元情報を指定した背景は「画像のみ」で確認できます。'
                '投稿全体は元情報の指定なしで表示しています。')
        self.assertNotIn(note, render_html(manifest))
        case = manifest['cases'][0]
        case['cancel_reference_path'] = 'records/confirmed-reference.json'
        case['cancel_reference_note'] = '合成訓練例で明示した参照です。'
        content = render_html(manifest)
        self.assertIn('<div class="post-view"><p class="post-note">' + note + '</p>', content)
        self.assertIn('href="records/confirmed-reference.json"', content)
        self.assertEqual(PostContents(content).bodies,
                         [with_image(case['posts'][0]['text'], PostContents.MEDIA)] * 2)

    def test_multiple_post_units_keep_separate_bodies_images_and_input_indexes(self):
        manifest = posting_sample(multiple=True)
        content = render_html(manifest)
        document = Document(content)
        units = [item for item in document.attrs('section') if item.get('class') == 'post-unit']
        self.assertEqual(len(units), 5)
        self.assertEqual([json.loads(item['data-record-indexes']) for item in units], [[i] for i in range(5)])
        self.assertEqual({item['data-post-kind'] for item in units}, {'single'})
        for index in range(1, 6):
            self.assertIn(f'<h3>投稿 {index} / 5</h3>', content)
            self.assertIn(f'入力 {index} · 1電文', content)
            # Two renderer versions each get the unchanged text and their own
            # local attachment. The original grouped image view also remains.
            self.assertEqual(sum(body.startswith(f'【訓練・試験】降灰予報 {index}\n')
                                 for body in PostContents(content).bodies), 2)
        sources = [image['src'] for image in document.attrs('img')]
        self.assertEqual(len(sources), 12)
        self.assertIn('images/ash-received-preview.png', sources)
        self.assertEqual(sources[2:], [f'images/post-{i}-{side}.png' for i in range(1, 6)
                                      for side in ('published', 'preview')])
        self.assertIn('<div class="post-frame"><div class="post-fold">', content)
        self.assertEqual(PostContents(content).bodies,
                         [with_image(post['text'], PostContents.MEDIA)
                          for post in manifest['cases'][0]['posts'] for _ in range(2)])
        self.assertNotIn('<summary>投稿本文を全文で確認</summary>', content)

    def test_collector_order_and_cancellation_or_release_text_are_not_rewritten(self):
        for text in ('\n【取消】気象情報\nこの情報は取り消されました。\n',
                     '【解除】洪水情報\n警報解除の受信値。',
                     '【訓練・試験】台風情報\n全時点をまとめた投稿。'):
            with self.subTest(text=text):
                manifest = posting_sample()
                post = manifest['cases'][0]['posts'][0]
                post.update(kind='typhoon', text=text, record_indexes=[4, 2, 0], record_count=3)
                content = render_html(manifest)
                self.assertEqual(PostContents(content).bodies, [with_image(text, PostContents.MEDIA)] * 2)
                self.assertIn('入力 5・3・1 · 3電文', content)
                self.assertNotIn('<summary>末尾のNMEAを表示</summary>', content)

    def test_nmea_stays_in_the_normal_body_and_media_uses_the_live_formatter_position(self):
        for text in ('本文\n---\nNMEA: $QZQSM,55,123*AB\nNMEA: $QZQSM,55,456*CD\n',
                     '本文\n---\n本文の中の区切り\n---\nNMEA: $QZQSM,55,123*AB\n',
                     '本文\n---\n注記を含む本文', '本文\n---\nNMEA: example',
                     '本文\n---\nNMEA: $QZQSM,55,123*AB\n後続の本文',
                     '本文\n---\nNMEA: $QZQSM,55,123*AB\n\n', '区切りのない本文\n',
                     '__SHIRASU_LOCAL_ATTACHMENT__\n__SHIRASU_LOCAL_ATTACHMENT___\n---\nNMEA: 例',
                     '\n<本文 & 引用>\n\n---\nNMEA: $QZQSM,55,123*AB\n'):
            with self.subTest(text=text):
                manifest = posting_sample()
                manifest['cases'][0]['posts'][0]['text'] = text
                content = render_html(manifest)
                self.assertEqual(PostContents(content).bodies, [with_image(text, PostContents.MEDIA)] * 2)
                self.assertNotIn('class="post-nmea"', content)
                self.assertNotIn('class="post-full"', content)
                self.assertNotIn('class="post-raw"', content)
                self.assertNotIn('末尾のNMEAを表示', content)
                self.assertIn('<summary>出典・入力データを確認</summary>', content)
        self.assertEqual(_post_content('本文\n---\nNMEA: 例', '<media>'), '本文\n\n<media>\n---\nNMEA: 例')
        self.assertEqual(_post_content('本文\n', '<media>'), '本文\n\n\n<media>')

    def test_whole_posts_offer_accessible_folding_and_remain_readable_without_javascript(self):
        manifest = posting_sample(multiple=True)
        content = render_html(manifest)
        document = Document(content)
        bodies = [attrs for attrs in document.attrs('div') if attrs.get('class') == 'post-body']
        controls = [attrs for attrs in document.attrs('button') if attrs.get('class') == 'post-toggle']
        folds = [attrs for attrs in document.attrs('div') if attrs.get('class') == 'post-fold']
        self.assertEqual(len(folds), 10)
        self.assertEqual(len({body['id'] for body in bodies}), 10)
        self.assertEqual([button['aria-controls'] for button in controls], [body['id'] for body in bodies])
        for button in controls:
            self.assertEqual(button['type'], 'button')
            self.assertEqual(button['aria-expanded'], 'true')
            self.assertIn('hidden', button)
        for attrs in bodies + folds:
            self.assertNotIn('hidden', attrs)
            self.assertNotIn('data-folded', attrs, 'Only working JavaScript may enable clipping')
            self.assertNotIn('style', attrs)
        self.assertIn('.post-fold[data-folded="true"] .post-clip{max-height:30rem;overflow:hidden}', content)
        self.assertIn('nostterの通常TL相当', content)
        self.assertIn('もっと見る', content)
        self.assertIn('元に戻す', content)
        self.assertIn('max-width:calc(100% - 1.5em);max-height:20em;margin:.5em;', content)

    def test_text_only_and_no_post_are_distinct_from_legacy_unrecorded_posts(self):
        manifest = posting_sample()
        case = manifest['cases'][0]
        post = case['posts'][0]
        post.update(kind='nankai', image_enabled=False, images={},
                    text='南海トラフの投稿本文\n\n---\nNMEA: $QZQSM,55,123*AB\n')
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual(len(document.attrs('img')), 2, 'Only the legacy image view keeps its images')
        self.assertIn('<h4>本文のみ', content)
        self.assertIn('南海トラフの投稿本文', content)
        self.assertEqual(PostContents(content).bodies, [post['text']])
        self.assertEqual(json.loads(document.attrs('article')[0]['data-post-reason-codes']), ['__text_only__'])
        self.assertIn('data-reason-count="__text_only__">1</strong>', content)
        case['posts'] = []
        content = render_html(manifest)
        self.assertIn('この入力からの投稿はありません。', content)
        self.assertNotIn('このサンプルには投稿全体の記録がありません。', content)
        self.assertEqual(json.loads(Document(content).attrs('article')[0]['data-post-reason-codes']), ['__no_post__'])
        self.assertIn('data-reason-count="__no_post__">1</strong>', content)
        del case['posts']
        content = render_html(manifest)
        self.assertIn('このサンプルには投稿全体の記録がありません。', content)
        self.assertNotIn('この入力からの投稿はありません。', content)

    def test_post_reasons_are_union_per_case_and_switch_separately_from_image_only_reasons(self):
        manifest = posting_sample(multiple=True)
        case = manifest['cases'][0]
        case['images']['preview']['decision'] = {'mode': 'map', 'reasons': []}
        why = {'code': 'cancel_scope_unavailable', 'stage': 'area', 'message': '取消区域が不明', 'detail': ''}
        for post in case['posts']:
            post['images']['preview']['decision'] = {'mode': 'summary', 'reasons': [why]}
        content = render_html(manifest)
        article, = Document(content).attrs('article')
        self.assertEqual(json.loads(article['data-reason-codes']), ['__unchanged__'])
        self.assertEqual(json.loads(article['data-post-reason-codes']), ['cancel_scope_unavailable'])
        self.assertIn('data-reason-count="cancel_scope_unavailable">1</strong>', content)
        self.assertIn('data-reason-count="__unchanged__">0</strong>', content)
        # View controls live outside the filter form, so filter reset keeps
        # both the chosen presentation mode and the chosen SNS width.
        self.assertLess(content.index('id="view-mode"'), content.index('<form id="filters"'))
        self.assertLess(content.index('id="image-width"'), content.index('<form id="filters"'))

    def test_post_errors_and_hostile_text_are_escaped_and_local_attachments_are_checked(self):
        manifest = posting_sample()
        attack = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'
        post = manifest['cases'][0]['posts'][0]
        post['text'] = attack + '\n---\nNMEA: $QZQSM,55,123*AB'
        post['images']['preview'] = {'error': attack}
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual(len(document.attrs('script')), 1)
        self.assertEqual(len(document.attrs('img')), 3)
        self.assertNotIn(attack, content)
        self.assertIn('&lt;/script&gt;', content)
        self.assertIn('添付画像を描画できませんでした', content)
        self.assertEqual(PostContents(content).bodies, [with_image(post['text'], PostContents.MEDIA)] * 2)
        self.assertEqual(json.loads(document.attrs('article')[0]['data-post-reason-codes']), ['__error__'])
        for _, attrs in document.elements:
            self.assertNotIn('onerror', attrs)
        for path in ('../post.png', '/tmp/post.png', 'https://example.com/post.png', '//example.com/post.png',
                     'images/%2e%2e/post.png', 'images/./post.png', 'images\\post.png', 'images//post.png'):
            with self.subTest(path=path):
                bad = posting_sample()
                bad['cases'][0]['posts'][0]['images']['preview']['path'] = path
                with self.assertRaises(ValueError):
                    render_html(bad)

    def test_invalid_post_units_and_attachment_dimensions_are_rejected(self):
        for field, value in (('kind', 'invented'), ('text', None), ('record_indexes', []),
                             ('record_indexes', [True]), ('record_indexes', [-1]), ('record_indexes', [5]),
                             ('record_indexes', [0, 0]), ('record_count', 2), ('record_count', True),
                             ('image_enabled', 1), ('images', [])):
            with self.subTest(field=field, value=value):
                manifest = posting_sample()
                manifest['cases'][0]['posts'][0][field] = value
                with self.assertRaises(ValueError):
                    render_html(manifest)
        for width in (0, -1, True, '360'):
            with self.subTest(width=width):
                manifest = posting_sample()
                manifest['cases'][0]['posts'][0]['images']['preview']['width'] = width
                with self.assertRaises(ValueError):
                    render_html(manifest)
        manifest = posting_sample()
        manifest['cases'][0]['posts'][0]['image_enabled'] = False
        with self.assertRaises(ValueError):
            render_html(manifest)

    def test_baseline_compares_preview_snapshots_without_inserting_the_published_version(self):
        manifest = baseline_sample()
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual(document.attrs('body'), [{'data-view-mode': 'posts', 'data-baseline': 'true'}])
        self.assertIn('前回の作業版と今回の作業版を比較できます。', content)
        self.assertIn('前回のコード：<code>previous456</code> · 未コミットの変更なし', content)
        self.assertIn('今回のコード：<code>current123</code> · 未コミットの変更を含む', content)
        self.assertIn('href="baseline/manifest.json"', content)
        self.assertNotIn('公開済み v4', content)
        self.assertNotIn('<h3>公開版', content)
        self.assertEqual([image['src'] for image in document.attrs('img')], [
            'baseline/images/ash-received-preview.png', 'images/ash-received-preview.png',
            'baseline/images/post-1-preview.png', 'images/post-1-preview.png',
        ])
        self.assertIn('<section class="comparison previous">', content)
        self.assertIn('<section class="comparison current">', content)
        self.assertIn('<h3>前回の作業版 ', content)
        self.assertIn('<h3>今回の作業版 ', content)
        self.assertEqual(content.count('class="baseline-column-heading"'), 2)
        self.assertIn('<details class="source-details baseline-source-details">', content)
        self.assertIn('href="baseline/records/ash-received.json"', content)

    def test_baseline_posts_preserve_each_sides_units_text_and_distinct_expansion_ids(self):
        manifest = baseline_sample(multiple=True)
        case = manifest['cases'][0]
        previous = case['comparison']['previous']
        previous['posts'][0]['text'] = '前回の本文\n---\nNMEA: 前回\n'
        case['posts'] = case['posts'][:2]
        case['posts'][0]['text'] = '今回の本文\n\n---\nNMEA: 今回\n'
        case['comparison']['posts'].update(status='changed', changes=['structure', 'text'],
                                          note='投稿数と本文が変わりました。')
        content = render_html(manifest)
        document = Document(content)
        expected_posts = previous['posts'] + case['posts']
        self.assertEqual(PostContents(content).bodies,
                         [with_image(post['text'], PostContents.MEDIA) for post in expected_posts])
        self.assertIn('<h3>投稿 1 / 5</h3>', content)
        self.assertIn('<h3>投稿 1 / 2</h3>', content)
        self.assertIn('投稿番号は対応関係を示しません。', content)
        self.assertIn('<span class="comparison-change">投稿構成</span>', content)
        self.assertIn('<span class="comparison-change">投稿本文</span>', content)
        bodies = [attrs for attrs in document.attrs('div') if attrs.get('class') == 'post-body']
        controls = [attrs for attrs in document.attrs('button') if attrs.get('class') == 'post-toggle']
        self.assertEqual(len(bodies), 7)
        self.assertTrue(all(body['id'].startswith('previous_') for body in bodies[:5]))
        self.assertTrue(all(body['id'].startswith('current_') for body in bodies[5:]))
        ids = [attrs['id'] for _, attrs in document.elements if 'id' in attrs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([button['aria-controls'] for button in controls], [body['id'] for body in bodies])
        self.assertNotIn('class="post-nmea"', content)

    def test_baseline_comparison_status_and_current_reasons_are_separate_and_mode_specific(self):
        manifest = baseline_sample()
        case = manifest['cases'][0]
        previous = case['comparison']['previous']
        case['images']['preview']['decision'] = {'mode': 'map', 'reasons': []}
        case['posts'][0]['images']['preview']['decision'] = {'mode': 'map', 'reasons': []}
        previous_reason = {'code': 'previous_only', 'message': '前回だけの表示理由', 'stage': 'area', 'detail': ''}
        previous['images']['preview']['decision'] = {'mode': 'summary', 'reasons': [previous_reason]}
        previous['posts'][0]['images']['preview']['decision'] = {'mode': 'summary', 'reasons': [previous_reason]}
        content = render_html(manifest)
        document = Document(content)
        article, = document.attrs('article')
        self.assertEqual(article['data-comparison-status'], 'changed')
        self.assertEqual(article['data-post-comparison-status'], 'unchanged')
        self.assertEqual(json.loads(article['data-reason-codes']), ['__unchanged__'])
        self.assertEqual(json.loads(article['data-post-reason-codes']), ['__unchanged__'])
        self.assertIn('前回だけの表示理由', content)
        self.assertNotIn('value="previous_only"', content)
        self.assertNotIn('data-reason-count="previous_only"', content)
        self.assertIn('表示理由・記録状態（今回の作業版）', content)
        self.assertIn('集計対象：投稿全体の添付画像（今回の作業版）', content)
        self.assertIn('data-comparison-count="unchanged">1</strong>', content)
        self.assertIn('data-comparison-count="changed">0</strong>', content)
        compare_options = content.split('<select id="comparison-status"', 1)[1].split('</select>', 1)[0]
        self.assertIn('<option value="">すべて</option>', compare_options)
        self.assertNotIn(' selected', compare_options)
        for status in ('changed', 'unchanged', 'input_changed', 'added', 'unavailable'):
            self.assertIn(f'<option value="{status}"', compare_options)
        self.assertIn('data-comparison-mode="images"', content)
        self.assertIn('data-comparison-mode="posts"', content)

    def test_baseline_different_input_is_explicit_and_unmatched_cases_are_outside_current_cards(self):
        manifest = baseline_sample()
        result = manifest['cases'][0]['comparison']['images']
        result.update(status='input_changed', changes=[], note='入力電文または基準時刻が前回と異なります。')
        content = render_html(manifest)
        document = Document(content)
        self.assertIn('<span class="comparison-status input_changed">入力が異なる</span>', content)
        self.assertIn('入力電文または基準時刻が前回と異なります。', content)
        self.assertIn('今回の比較対象にない前回ケース（1件）', content)
        self.assertIn('今回選択していない前回の例', content)
        self.assertNotIn('削除された', content)
        self.assertEqual(len(document.attrs('article')), 1)
        self.assertNotIn('id="other-previous"', content)

    def test_missing_previous_case_and_legacy_post_data_are_not_fabricated(self):
        manifest = baseline_sample()
        case = manifest['cases'][0]
        previous = case['comparison']['previous']
        del previous['posts']
        previous['post_text'] = '古い単独本文の記録'
        case['comparison']['posts'].update(status='unavailable', changes=[], note='前回の投稿記録がありません。')
        content = render_html(manifest)
        self.assertIn('この作業版には投稿全体の記録がありません。', content)
        self.assertIn('前回の投稿記録がありません。', content)
        self.assertEqual(PostContents(content).bodies,
                         [with_image(case['posts'][0]['text'], PostContents.MEDIA)])
        self.assertIn('<h4>前回の投稿本文</h4><pre>古い単独本文の記録</pre>', content)
        case['comparison']['previous'] = None
        for mode in ('images', 'posts'):
            case['comparison'][mode].update(status='added', changes=[], note='今回初めて選択したケースです。')
        content = render_html(manifest)
        self.assertIn('前回の対応ケースがありません。', content)
        self.assertNotIn('baseline/images/', content)
        self.assertNotIn('<details class="source-details baseline-source-details">', content)
        self.assertIn('<span class="comparison-status added">今回追加</span>', content)

    def test_baseline_can_show_previous_posts_when_current_has_no_post_or_no_post_data(self):
        manifest = baseline_sample()
        case = manifest['cases'][0]
        previous = case['comparison']['previous']
        case['posts'] = []
        content = render_html(manifest)
        self.assertIn('この入力からの投稿はありません。', content)
        self.assertEqual(PostContents(content).bodies,
                         [with_image(previous['posts'][0]['text'], PostContents.MEDIA)])
        del case['posts']
        content = render_html(manifest)
        self.assertEqual(Document(content).attrs('body')[0]['data-view-mode'], 'posts')
        self.assertIn('この作業版には投稿全体の記録がありません。', content)
        self.assertEqual(Document(content).attrs('article')[0]['data-has-posts'], 'true')
        self.assertEqual(PostContents(content).bodies,
                         [with_image(previous['posts'][0]['text'], PostContents.MEDIA)])
        del previous['posts']
        content = render_html(manifest)
        self.assertEqual(Document(content).attrs('body')[0]['data-view-mode'], 'images')
        self.assertIn('前回の作業版と今回の作業版の画像を比較できます。', content)
        self.assertEqual(PostContents(content).bodies, [])

    def test_baseline_text_and_metadata_are_escaped_and_only_local_assets_can_be_linked(self):
        attack = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'
        manifest = baseline_sample()
        case = manifest['cases'][0]
        previous = case['comparison']['previous']
        previous.update(title=attack, source_file=attack, report_text=attack,
                        cancel_reference_path='baseline/records/reference.json', cancel_reference_note=attack)
        previous['posts'][0]['text'] = attack + '\n---\nNMEA: 例'
        case['comparison']['images'].update(note=attack, changes=[attack])
        manifest['baseline'].update(commit=attack, unmatched_cases=[{'slug': attack, 'title': attack}])
        content = render_html(manifest)
        document = Document(content)
        self.assertNotIn(attack, content)
        self.assertEqual(len(document.attrs('script')), 1)
        self.assertEqual(len(document.attrs('img')), 4)
        self.assertTrue(all('onerror' not in attrs for _, attrs in document.elements))
        self.assertIn('href="baseline/records/reference.json"', content)
        self.assertEqual(PostContents(content).bodies[0], with_image(previous['posts'][0]['text'], PostContents.MEDIA))
        for field in ('manifest', 'image', 'post_image', 'records', 'reference'):
            for path in ('../baseline.json', '/tmp/baseline.json', 'https://example.com/asset', 'baseline/%2e%2e/image.png'):
                with self.subTest(field=field, path=path):
                    bad = baseline_sample()
                    old = bad['cases'][0]['comparison']['previous']
                    if field == 'manifest':
                        bad['baseline']['manifest_path'] = path
                    elif field == 'image':
                        old['images']['preview']['path'] = path
                    elif field == 'post_image':
                        old['posts'][0]['images']['preview']['path'] = path
                    elif field == 'records':
                        old['records_path'] = path
                    else:
                        old['cancel_reference_path'] = path
                    with self.assertRaises(ValueError):
                        render_html(bad)

    def test_invalid_comparison_metadata_is_rejected(self):
        for field, value in (('status', 'invented'), ('changes', 'image'), ('changes', [None]), ('note', None)):
            with self.subTest(field=field, value=value):
                manifest = baseline_sample()
                manifest['cases'][0]['comparison']['images'][field] = value
                with self.assertRaises(ValueError):
                    render_html(manifest)

    def test_recorded_alt_is_used_verbatim_on_images_and_reviewed_outside_post_frames(self):
        manifest = posting_sample()
        case = manifest['cases'][0]
        values = [case['images'][side] for side in ('published', 'preview')]
        values += [case['posts'][0]['images'][side] for side in ('published', 'preview')]
        descriptions = [f'【訓練】画像 {index} の内容\n対象は「北海道」と青森県。\n' for index in range(4)]
        for value, description in zip(values, descriptions):
            value['alt'] = description
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual([image['alt'] for image in document.attrs('img')], descriptions)
        self.assertEqual(len([attrs for attrs in document.attrs('details')
                              if attrs.get('class') == 'image-alt']), 4)
        for description in descriptions:
            self.assertIn('<div class="image-alt-text">' + description + '</div>', content)
        self.assertEqual(PostContents(content).bodies,
                         [with_image(case['posts'][0]['text'], PostContents.MEDIA)] * 2)
        self.assertEqual(content.count('</button></div></div></div><details class="image-alt">'), 2)
        image_links = [attrs for attrs in document.attrs('a')
                       if attrs.get('class') in ('image-link', 'post-attachment')]
        for link, description in zip(image_links, descriptions):
            self.assertIn(description, link['aria-label'], 'Link labels must not mask the recorded image description')

    def test_legacy_alt_attributes_are_preserved_without_inventing_a_recorded_description(self):
        manifest = posting_sample()
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual([image['alt'] for image in document.attrs('img')], [
            '降灰予報 / 6時間分 / 公開版 / 要旨', '降灰予報 / 6時間分 / 作業版 / 地図',
            '降灰予報 / 6時間分 / 公開版の添付画像', '降灰予報 / 6時間分 / 作業版の添付画像',
        ])
        self.assertEqual(content.count('この版にはALTの記録がありません。'), 4)
        self.assertEqual(len([attrs for attrs in document.attrs('details')
                              if attrs.get('class') == 'image-alt unrecorded']), 4)
        self.assertNotIn('<div class="image-alt-text">', content)

    def test_alt_text_cannot_inject_markup_or_escape_its_attribute(self):
        manifest = posting_sample()
        attack = '説明" onerror="alert(1)\n</script><script>alert(2)</script><img src=x>'
        case = manifest['cases'][0]
        for owner in (case, case['posts'][0]):
            for image in owner['images'].values():
                image['alt'] = attack
        content = render_html(manifest)
        document = Document(content)
        self.assertEqual(len(document.attrs('img')), 4)
        self.assertEqual(len(document.attrs('script')), 1)
        self.assertTrue(all('onerror' not in attrs for _, attrs in document.elements))
        self.assertEqual([image['alt'] for image in document.attrs('img')], [attack] * 4)
        self.assertNotIn(attack, content)
        self.assertEqual(PostContents(content).bodies,
                         [with_image(case['posts'][0]['text'], PostContents.MEDIA)] * 2)

    def test_alt_review_distinguishes_recording_changes_without_reconstructing_previous_alt(self):
        manifest = baseline_sample()
        case = manifest['cases'][0]
        case['images']['preview']['alt'] = '今回の画像に記録された説明'
        case['posts'][0]['images']['preview']['alt'] = '今回の添付画像に記録された説明'
        case['comparison']['images'].update(status='changed', changes=['alt'], note='')
        case['comparison']['posts'].update(status='changed', changes=['alt_recording'], note='')
        content = render_html(manifest)
        alts = [image['alt'] for image in Document(content).attrs('img')]
        self.assertEqual(alts[1], case['images']['preview']['alt'])
        self.assertEqual(alts[3], case['posts'][0]['images']['preview']['alt'])
        self.assertNotEqual(alts[0], alts[1])
        self.assertNotEqual(alts[2], alts[3])
        self.assertEqual(content.count('この版にはALTの記録がありません。'), 2)
        self.assertIn('<span class="comparison-change">代替テキスト（ALT）</span>', content)
        self.assertIn('<span class="comparison-change">ALTの記録状態</span>', content)

    def test_empty_or_non_string_alt_is_invalid_even_for_error_cells(self):
        for value in ('', ' \n\t', None, 0, False, [], {}):
            for target in ('case', 'post', 'error'):
                with self.subTest(value=value, target=target):
                    manifest = posting_sample()
                    case = manifest['cases'][0]
                    owner = case['posts'][0] if target == 'post' else case
                    if target == 'error':
                        owner['images']['preview'] = {'error': 'Rendering failed'}
                    owner['images']['preview']['alt'] = value
                    with self.assertRaisesRegex(ValueError, 'ALT'):
                        render_html(manifest)


if __name__ == '__main__':
    unittest.main()
