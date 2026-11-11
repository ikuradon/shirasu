"""Self-contained, file:// compatible HTML for the offline image review tool."""

from html import escape
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit

from image_publication import with_image


CATEGORIES = {
    'Typhoon': '台風', 'Hypocenter': '震源情報', 'Weather': '気象',
    'EarthquakeEarlyWarning': '緊急地震速報', 'SeismicIntensity': '震度情報',
    'Tsunami': '津波', 'Volcano': '火山', 'AshFall': '降灰', 'Flood': '洪水',
    'NorthwestPacificTsunami': '北西太平洋津波', 'Marine': '海上警報',
    'LAlert': 'DCX / Lアラート', 'JAlert': 'DCX / Jアラート',
    'MTInfo': 'DCX / 自治体情報', 'OutsideJapan': 'DCX / 日本国外',
    'NankaiTroughEarthquake': '南海トラフ',
}
SOURCES = {'receiver': '実受信', 'archive': 'アーカイブ由来', 'synthetic': '合成'}
CLASSIFICATIONS = {'training': '訓練・試験', 'ordinary': '通常配信'}
STATES = {'issue': '発表', 'release': '解除', 'cancel': '取消'}
KINDS = {'map': '地図', 'summary': '要旨', 'topic': 'トピック', 'text': 'テキスト'}
MODES = {'map': '地図', 'geometry': '地点・範囲', 'area': '対象区域',
         'summary': '要旨', 'topic': 'トピック'}
STAGES = {'selection': '表示の選択', 'geometry': '地点・範囲', 'area': '対象区域',
          'layout': '配置', 'render': '描画'}
COMPARISON_STATUSES = {'changed': '変更あり', 'unchanged': '変更なし',
                       'input_changed': '入力が異なる', 'added': '今回追加',
                       'unavailable': '比較できない'}
COMPARISON_CHANGES = {'image': '画像', 'mode': '表示方法', 'reasons': '表示理由',
                      'text': '投稿本文', 'structure': '投稿構成', 'conditions': '投稿プレビューの前提',
                      'alt': '代替テキスト（ALT）', 'alt_recording': 'ALTの記録状態'}


def _escape(value):
    return escape(str(value), quote=True)


def _local_path(value):
    """Only link to generated relative assets, never remote or parent paths."""
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', value)
            or any(part in ('', '.', '..') for part in value.split('/'))
            or PurePosixPath(value).is_absolute()):
        raise ValueError('Gallery assets must have safe relative paths')
    return value


def _source_url(value):
    if not isinstance(value, str) or any(ord(c) < 32 or c.isspace() for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == 'https' and parsed.hostname and not parsed.username
                and not parsed.password and '\\' not in value):
            return value
    except ValueError:
        pass
    return None


def _options(values, labels):
    return ''.join(f'<option value="{_escape(value)}">{_escape(labels.get(value, value))}</option>'
                   for value in dict.fromkeys(values))


def _reason_items(image):
    """Count cases by recorded preview reasons, never infer from an image's kind."""
    if image.get('error'):
        return [('__error__', '描画エラー')]
    decision = image.get('decision')
    if not isinstance(decision, dict):
        return [('__unrecorded__', '理由記録なし')]
    reasons = decision.get('reasons', ())
    if not reasons:
        return [('__unchanged__', '切替なし（対応した表示）')]
    return [(str(item['code']), str(item.get('message') or item['code'])) for item in reasons]


def _post_reason_items(case):
    if 'posts' not in case:
        return _reason_items(case['images'].get('preview', {}))
    if not case['posts']:
        return [('__no_post__', '投稿対象なし')]
    items = []
    for post in case['posts']:
        items.extend(_reason_items(post['images'].get('preview', {})) if post['image_enabled']
                     else [('__text_only__', '本文のみ（画像を添付しない投稿）')])
    return list(dict(items).items())


def _post_content(text, attachment=None):
    """Replace only the live formatter's image URL position with local media."""
    if attachment is None:
        return _escape(text)
    marker = '__SHIRASU_LOCAL_ATTACHMENT__'
    while marker in text:
        marker += '_'
    before, _, after = with_image(text, marker).partition(marker)
    return _escape(before) + attachment + _escape(after)


def _recorded_alt(image):
    if 'alt' not in image:
        return None
    value = image['alt']
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Image ALT must be a nonempty string')
    return value


def _alt_details(image):
    value = _recorded_alt(image)
    if value is None:
        return ('<details class="image-alt unrecorded"><summary>代替テキスト（ALT）'
                '<span class="alt-recording">記録なし</span></summary>'
                '<p>この版にはALTの記録がありません。</p></details>')
    return ('<details class="image-alt"><summary>代替テキスト（ALT）</summary>'
            f'<div class="image-alt-text">{_escape(value)}</div></details>')


def _post_attachment(case, value, heading):
    recorded_alt = _recorded_alt(value)
    if value.get('error'):
        return ('<span class="post-attachment-error"><strong>添付画像を描画できませんでした</strong>'
                f'<span class="post-attachment-error-detail">{_escape(value["error"])}</span></span>')
    if 'path' not in value:
        return '<span class="post-attachment-error">添付画像の記録がありません。</span>'
    path = _local_path(value['path'])
    width, height = value.get('width'), value.get('height')
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError('Post attachment dimensions must be positive integers')
    label = _escape(f'{case["title"]} / {heading}の添付画像')
    alt = _escape(recorded_alt) if recorded_alt is not None else label
    link_label = f'{alt}（画像を原寸で開く）' if recorded_alt is not None else f'{label}を原寸で開く'
    return (f'<a class="post-attachment" href="{path}" target="_blank" rel="noopener" '
            f'aria-label="{link_label}"><img src="{path}" width="{width}" height="{height}" '
            f'alt="{alt}" loading="lazy" decoding="async"></a>')


def _posts(case, published_version, *, preview_only=False, id_prefix='', embedded=False):
    """Preserve real posting units, with local attachments inside each frame."""
    if 'posts' not in case:
        if embedded:
            return '<p class="baseline-unavailable">この作業版には投稿全体の記録がありません。</p>'
        return ('<p class="post-unavailable">このサンプルには投稿全体の記録がありません。'
                '画像のみ表示しています。</p>')
    posts = case['posts']
    if not isinstance(posts, list):
        raise ValueError('Gallery posts must be a list')
    reference_note = (
        '<p class="post-note">元情報を指定した背景は「画像のみ」で確認できます。'
        '投稿全体は元情報の指定なしで表示しています。</p>'
        if case.get('cancel_reference_path') else '')
    wrapper = 'post-sequence' if embedded else 'post-view'
    if not posts:
        return (f'<div class="{wrapper}">' + reference_note
                + '<p class="post-empty">この入力からの投稿はありません。</p></div>')
    units = []
    for index, post in enumerate(posts, 1):
        indexes = post.get('record_indexes')
        if (post.get('kind') not in ('single', 'typhoon', 'nankai')
                or not isinstance(post.get('text'), str)
                or not isinstance(indexes, list) or not indexes
                or any(type(number) is not int or not 0 <= number < case['record_count'] for number in indexes)
                or len(set(indexes)) != len(indexes)
                or type(post.get('record_count')) is not int or post['record_count'] != len(indexes)
                or type(post.get('image_enabled')) is not bool
                or not isinstance(post.get('images'), dict)):
            raise ValueError('Invalid gallery post unit')
        record_labels = '・'.join(str(number + 1) for number in indexes)
        image_indexes = post.get('image_record_indexes', indexes if post['image_enabled'] else [])
        text_slots = post.get('text_only_slots', [])
        if (not isinstance(image_indexes, list)
                or any(type(number) is not int or number not in indexes for number in image_indexes)
                or len(set(image_indexes)) != len(image_indexes)
                or bool(image_indexes) != post['image_enabled']
                or not isinstance(text_slots, list)
                or any(side not in ('published', 'preview') for side in text_slots)
                or len(set(text_slots)) != len(text_slots)
                or any(side in post['images'] for side in text_slots)):
            raise ValueError('Invalid gallery post image inputs or slots')
        if post['image_enabled']:
            sides = ['preview'] if preview_only else [side for side in ('published', 'preview')
                                                      if side in post['images'] or side in text_slots]
            if not sides:
                sides = ['preview']
        else:
            if post['images']:
                raise ValueError('A text-only post must not carry an attachment')
            sides = ['text']
        comparisons = []
        for side in sides:
            value = post['images'].get(side, {})
            enabled = post['image_enabled'] and side not in text_slots
            heading = {'published': '公開版', 'preview': '作業版', 'text': '本文のみ'}[side]
            if side in text_slots:
                heading += '（本文のみ）'
            if embedded and side != 'text':
                heading = '添付画像' if enabled else '本文のみ'
            version = (value.get('version', published_version if side == 'published' else 'preview')
                       if side != 'text' else '')
            attachment = _post_attachment(case, value, heading) if enabled else None
            content = _post_content(post['text'], attachment)
            content_id = _escape(f'{id_prefix}{case["slug"]}-post-{index}-{side}')
            diagnostic = _decision(value) if enabled and not value.get('error') else ''
            alt_details = _alt_details(value) if enabled else ''
            comparisons.append(
                f'<section class="post-comparison {side}"><div class="image-heading">'
                f'<h4>{heading}<span class="version">{_escape(version)}</span></h4></div>'
                '<div class="post-viewport" role="group" aria-label="投稿本文と添付画像の表示領域">'
                '<div class="post-frame"><div class="post-fold"><div class="post-clip">'
                f'<div class="post-body" id="{content_id}">{content}</div></div>'
                f'<button class="post-toggle" type="button" aria-controls="{content_id}" '
                'aria-expanded="true" hidden>元に戻す</button></div></div></div>'
                f'{alt_details}{diagnostic}</section>')
        grid = 'post-grid single' if len(sides) == 1 else 'post-grid'
        image_note = (f' · 画像は入力 {"・".join(str(number + 1) for number in image_indexes)} の見出し'
                      if post['kind'] == 'nankai' and post['image_enabled'] else '')
        units.append(
            f'<section class="post-unit" data-post-kind="{post["kind"]}" '
            f'data-record-indexes="{_escape(json.dumps(indexes))}">'
            f'<div class="post-unit-heading"><h3>投稿 {index} / {len(posts)}</h3>'
            f'<p>入力 {record_labels} · {post["record_count"]}電文{image_note}</p></div>'
            f'<div class="{grid}">{"".join(comparisons)}</div></section>')
    return f'<div class="{wrapper}">' + reference_note + ''.join(units) + '</div>'


def _decision(image):
    value = image.get('decision')
    if not isinstance(value, dict):
        return '<div class="display-decision unrecorded"><p>表示理由：記録なし</p></div>'
    mode = value.get('mode', '')
    heading = f'<p class="decision-heading">表示方法：{_escape(MODES.get(mode, mode))}</p>'
    reasons = []
    for item in value.get('reasons', ()):
        stage = item.get('stage', '')
        detail = (f'<p class="reason-detail">{_escape(item["detail"])}</p>'
                  if item.get('detail') else '')
        reasons.append(f'<li><span class="reason-stage">{_escape(STAGES.get(stage, stage))}</span> '
                       f'{_escape(item.get("message") or item["code"])}{detail}</li>')
    content = ('<ul>' + ''.join(reasons) + '</ul>' if reasons
               else '<p class="decision-unchanged">切替なし（対応した表示）</p>')
    return f'<div class="display-decision">{heading}{content}</div>'


def _image(case, side, published_version, *, heading=None, column=None):
    value = case['images'][side]
    recorded_alt = _recorded_alt(value)
    alt_details = _alt_details(value)
    heading = heading or ('公開版' if side == 'published' else '作業版')
    column = column or side
    version = value.get('version', published_version if side == 'published' else 'preview')
    kind = KINDS.get(value.get('kind'), '描画')
    top = (f'<div class="image-heading"><h3>{heading} '
           f'<span class="version">{_escape(version)}</span></h3>'
           f'<span class="kind">{_escape(kind)}</span></div>')
    if value.get('error'):
        content = ('<div class="render-error"><strong>描画できませんでした</strong>'
                   f'<p>{_escape(value["error"])}</p></div>')
        return f'<section class="comparison {column}">{top}{content}{alt_details}</section>'
    path = _local_path(value['path'])
    width, height = value.get('width', 1440), value.get('height', 1200)
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError('Gallery image dimensions must be positive integers')
    label = _escape(f'{case["title"]} / {heading} / {kind}')
    alt = _escape(recorded_alt) if recorded_alt is not None else label
    link_label = f'{alt}（画像を原寸で開く）' if recorded_alt is not None else f'{label}を原寸で開く'
    content = (f'<div class="image-viewport" role="group" aria-label="{label}の表示領域">'
               f'<a class="image-link" href="{path}" target="_blank" rel="noopener" '
               f'aria-label="{link_label}">'
               f'<img src="{path}" width="{width}" height="{height}" '
               f'alt="{alt}" loading="lazy" decoding="async"></a></div>'
               f'<div class="image-footer"><span>元画像 {width} × {height}</span>'
               f'<a href="{path}" target="_blank" rel="noopener">PNGを原寸で開く ↗</a></div>')
    return f'<section class="comparison {column}">{top}{content}{alt_details}{_decision(value)}</section>'


def _comparison_result(case, mode):
    comparison = case.get('comparison', {})
    if not isinstance(comparison, dict):
        raise ValueError('Gallery comparison must be an object')
    value = comparison.get(mode)
    if value is None:
        return {'status': 'unavailable', 'changes': [], 'note': '比較情報が記録されていません。'}
    if (not isinstance(value, dict) or value.get('status') not in COMPARISON_STATUSES
            or not isinstance(value.get('changes'), list)
            or any(not isinstance(change, str) for change in value['changes'])
            or not isinstance(value.get('note'), str)):
        raise ValueError('Invalid gallery comparison result')
    return value


def _comparison_notice(case, mode):
    value = _comparison_result(case, mode)
    status = value['status']
    changes = ''.join(f'<span class="comparison-change">{_escape(COMPARISON_CHANGES.get(change, change))}</span>'
                      for change in value['changes'])
    note = f'<p>{_escape(value["note"])}</p>' if value['note'] else ''
    return (f'<div class="comparison-notice" data-comparison-mode="{mode}">'
            f'<div><span class="comparison-status {status}">{COMPARISON_STATUSES[status]}</span>'
            f'{changes}</div>{note}</div>')


def _baseline_views(case, published_version):
    previous = case.get('comparison', {}).get('previous')
    if previous is not None and not isinstance(previous, dict):
        raise ValueError('Previous gallery case must be an object or null')
    images, posts = [], []
    for column, heading, version_case in (('previous', '前回の作業版', previous),
                                          ('current', '今回の作業版', case)):
        if version_case is not None and 'preview' in version_case.get('images', {}):
            images.append(_image(version_case, 'preview', published_version, heading=heading, column=column))
        else:
            message = '前回の対応ケースがありません。' if version_case is None else '作業版の画像が記録されていません。'
            images.append(f'<section class="comparison {column}"><div class="image-heading"><h3>{heading}</h3>'
                          f'</div><p class="baseline-unavailable">{message}</p></section>')
        body = ('<p class="baseline-unavailable">前回の対応ケースがありません。</p>' if version_case is None
                else _posts(version_case, published_version, preview_only=True,
                            id_prefix=f'{column}_{case["slug"]}_', embedded=True))
        posts.append(f'<section class="baseline-column {column}"><h3 class="baseline-column-heading">'
                     f'{heading}</h3>{body}</section>')
    image_view = ('<div class="image-only">' + _comparison_notice(case, 'images')
                  + '<div class="comparison-grid baseline-images">' + ''.join(images) + '</div></div>')
    post_view = ('<div class="post-view baseline-post-view">' + _comparison_notice(case, 'posts')
                 + '<p class="post-note">左右それぞれの投稿単位で表示しています。投稿番号は対応関係を示しません。</p>'
                 + '<div class="baseline-post-grid">' + ''.join(posts) + '</div></div>')
    return image_view + post_view


def _previous_source(case):
    previous = case.get('comparison', {}).get('previous')
    if previous is None:
        return ''
    paths = _local_path(previous['records_path'])
    reference = ''
    if previous.get('cancel_reference_path'):
        path = _local_path(previous['cancel_reference_path'])
        reference = (f'<p>{_escape(previous.get("cancel_reference_note", ""))}</p>'
                     f'<a class="records-link" href="{path}" target="_blank" rel="noopener">'
                     '前回の確認済み参照入力JSONを開く ↗</a>')
    text = ''.join(f'<h4>{label}</h4><pre>{_escape(previous[field])}</pre>'
                   for field, label in (('report_text', '前回の電文のテキスト'), ('post_text', '前回の投稿本文'))
                   if previous.get(field))
    return ('<details class="source-details baseline-source-details"><summary>前回の出典・入力データを確認</summary>'
            '<div class="details-content"><dl>'
            f'<div><dt>前回のケース</dt><dd>{_escape(previous["title"])}</dd></div>'
            f'<div><dt>元ファイル</dt><dd>{_escape(previous.get("source_file", ""))}</dd></div>'
            f'<div><dt>出典のSHA-256</dt><dd><code>{_escape(previous.get("source_sha256", ""))}</code></dd></div>'
            f'<div><dt>電文数</dt><dd>{_escape(previous["record_count"])}</dd></div></dl>'
            f'<a class="records-link" href="{paths}" target="_blank" rel="noopener">前回の入力JSONを開く ↗</a>'
            f'{reference}{text}</div></details>')


def _case(case, index, published_version, *, baseline=False):
    slug = case['slug']
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', slug):
        raise ValueError('Gallery case IDs must be lowercase slug names')
    if baseline:
        views = _baseline_views(case, published_version)
    else:
        sides = [side for side in ('published', 'preview') if side in case['images']]
        images = ''.join(_image(case, side, published_version) for side in sides)
        grid = 'comparison-grid single' if len(sides) == 1 else 'comparison-grid'
        views = f'<div class="image-only"><div class="{grid}">{images}</div></div>' + _posts(case, published_version)
    category = CATEGORIES.get(case['category'], case['category'])
    source = SOURCES.get(case['source_kind'], case['source_kind'])
    classification = CLASSIFICATIONS.get(case['classification'], case['classification'])
    state = STATES.get(case['state'], case['state'])
    tags = ''.join(f'<span class="tag">{_escape(tag)}</span>' for tag in case.get('tags', ()))
    attributes = ' '.join(f'data-{key.replace("_", "-")}="{_escape(case[key])}"'
                          for key in ('category', 'source_kind', 'classification', 'state'))
    reason_codes = list(dict.fromkeys(code for code, _ in _reason_items(case['images'].get('preview', {}))))
    attributes += f' data-reason-codes="{_escape(json.dumps(reason_codes, ensure_ascii=False))}"'
    post_reason_codes = [code for code, _ in _post_reason_items(case)]
    attributes += f' data-post-reason-codes="{_escape(json.dumps(post_reason_codes, ensure_ascii=False))}"'
    attributes += f' data-has-posts="{"true" if baseline or "posts" in case else "false"}"'
    if baseline:
        attributes += f' data-comparison-status="{_comparison_result(case, "images")["status"]}"'
        attributes += f' data-post-comparison-status="{_comparison_result(case, "posts")["status"]}"'
    source_label = case.get('source_label', case.get('source_file', '出典情報なし'))
    source_url = _source_url(case.get('source_url'))
    source_link = (f'<a href="{_escape(source_url)}" target="_blank" rel="noopener noreferrer">'
                   f'{_escape(source_label)} ↗</a>' if source_url else _escape(source_label))
    rows = [('出典', source_link), ('元ファイル', _escape(case.get('source_file', ''))),
            ('出典のSHA-256', f'<code>{_escape(case.get("source_sha256", ""))}</code>'),
            ('電文数', _escape(case['record_count'])),
            ('復号の基準時刻', '<br>'.join(_escape(value) for value in case.get('reference_times', ())))]
    metadata = ''.join(f'<div><dt>{key}</dt><dd>{value}</dd></div>' for key, value in rows if value)
    text = case.get('report_text', '')
    body = f'<h4>電文のテキスト</h4><pre>{_escape(text)}</pre>' if text else ''
    post_text = case.get('post_text')
    post = ('<details class="source-details post-details"><summary>投稿本文</summary>'
            f'<div class="details-content"><pre>{_escape(post_text)}</pre></div></details>\n  '
            if post_text else '')
    paths = _local_path(case['records_path'])
    reference = ''
    if case.get('cancel_reference_path'):
        reference_path = _local_path(case['cancel_reference_path'])
        reference = (f'<p class="date-note">{_escape(case.get("cancel_reference_note", ""))}</p>'
                     f'<a class="records-link" href="{reference_path}" target="_blank" rel="noopener">'
                     '確認済み参照入力JSONを開く ↗</a>')
    previous_source = _previous_source(case) if baseline else ''
    return f'''<article class="case" id="{slug}" {attributes}>
  <header class="case-heading">
    <div><p class="category"><span class="case-number">{index:02d}</span> {_escape(category)}</p>
    <h2>{_escape(case['title'])}</h2></div>
    <a class="permalink" href="#{slug}" aria-label="{_escape(case['title'])}へのリンク">#</a>
  </header>
  <div class="case-labels"><span class="source-label">{_escape(source)}</span>
    <span class="classification {_escape(case['classification'])}">{_escape(classification)}</span>
    <span class="state {_escape(case['state'])}">{_escape(state)}</span>{tags}
    <span class="record-count">{_escape(case['record_count'])}電文</span></div>
  <p class="description">{_escape(case['description'])}</p>
  {views}
  {post}<details class="source-details"><summary>出典・入力データを確認</summary>
    <div class="details-content"><dl>{metadata}</dl>
    <p class="date-note">基準時刻は電文の日付を復元するために使用します。このギャラリーの公開受信ログには、電文ごとの正確な受信時刻は記録されていません。</p>
    <a class="records-link" href="{paths}" target="_blank" rel="noopener">入力JSONを開く ↗</a>{reference}{body}</div>
  </details>{previous_source}
</article>'''


_CSS = '''
:root{color-scheme:light;--bg:#f3f5f7;--paper:#fff;--ink:#17232f;--muted:#576574;--line:#dce2e8;--blue:#1d608e;--preview:#64538d}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:1rem}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 system-ui,-apple-system,"Noto Sans CJK JP","Yu Gothic",sans-serif}
a{color:var(--blue);text-underline-offset:3px}button,input,select{font:inherit}button,select{cursor:pointer}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #3c87bd;outline-offset:4px}
.page-header{background:var(--paper);border-bottom:1px solid var(--line)}.header-inner,main,.page-footer{max-width:1520px;margin:auto;padding-left:40px;padding-right:40px}.header-inner{padding-top:34px;padding-bottom:25px}.heading-line{display:flex;justify-content:space-between;align-items:center;gap:20px}.heading-line h1{font-size:31px;letter-spacing:.03em;margin:0;line-height:1.4}.heading-line a{font-size:13px;text-decoration:none}.intro{margin:9px 0 0;color:var(--muted)}.notice{display:flex;align-items:center;gap:10px;font-size:13px;color:#66552d;margin:18px 0 0}.notice::before{content:"";width:7px;height:7px;border-radius:50%;background:#ae8a40;flex:none}
main{padding-top:24px;padding-bottom:50px}.toolbar{padding:20px;background:var(--paper);border:1px solid var(--line);border-radius:10px;margin-bottom:24px}.toolbar-top{display:flex;align-items:center;gap:16px;justify-content:space-between;margin-bottom:13px}.toolbar-top p{margin:0;color:var(--muted);font-size:13px}.toolbar-top strong{font-size:18px;color:var(--ink);font-variant-numeric:tabular-nums}.reset{border:0;background:none;color:var(--blue);padding:3px 0;text-decoration:underline;text-underline-offset:3px;font-size:13px}.filters{display:grid;grid-template-columns:minmax(210px,1.5fr) repeat(4,minmax(125px,1fr));gap:12px}.filters label{display:flex;flex-direction:column;gap:5px;font-size:12px;font-weight:650;color:var(--muted)}.filters input,.filters select{min-width:0;width:100%;padding:9px 11px;background:#fff;border:1px solid #cbd4dc;color:var(--ink);border-radius:6px;font-size:14px;line-height:1.5}.filters input::placeholder{color:#7d8a96;font-weight:400}.no-script{margin:0 0 20px;color:var(--muted)}
.case{background:var(--paper);border:1px solid var(--line);border-radius:12px;margin-bottom:26px;overflow:hidden;scroll-margin-top:20px}.case-heading{display:flex;justify-content:space-between;align-items:center;padding:24px 24px 0;gap:16px}.category{margin:0 0 3px;font-size:12px;color:var(--muted);font-weight:650;letter-spacing:.03em}.case-number{color:#8392a1;font-weight:400;font-variant-numeric:tabular-nums;margin-right:7px}.case h2{margin:0;font-size:23px;line-height:1.5;letter-spacing:.015em}.permalink{font-size:21px;color:#8795a3;text-decoration:none;padding:5px}.case-labels{display:flex;align-items:center;flex-wrap:wrap;gap:7px;padding:12px 24px 0;font-size:12px}.source-label,.classification,.state,.tag{display:inline-block;border:1px solid var(--line);padding:2px 8px;border-radius:4px;background:#fafbfc;line-height:1.7}.source-label{color:#415568}.classification.training{background:#fff8e9;border-color:#eadab6;color:#72541c}.classification.ordinary{background:#eff5f9;border-color:#d4e1eb;color:#345e7d}.state.release{color:#276857;background:#edf6f1;border-color:#d0e4d9}.state.cancel{color:#8a3946;background:#fcf0f2;border-color:#e9cdd3}.tag{color:var(--muted);border-color:transparent;background:#f0f3f6}.record-count{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}.description{margin:11px 24px 19px;color:var(--muted);font-size:14px;max-width:1100px}.comparison-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:0 24px 21px}.comparison{min-width:0}.image-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:10px}.image-heading h3{font-size:13px;margin:0;font-weight:700}.published .image-heading h3{color:var(--blue)}.preview .image-heading h3{color:var(--preview)}.version{font-size:11px;font-weight:500;padding-left:6px;color:var(--muted)}.kind{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:3px;padding:1px 6px}.image-link{display:block;background:#10171e;border-radius:5px;overflow:hidden;line-height:0;box-shadow:0 0 0 1px #17232f10}.image-link img{display:block;width:100%;height:auto;object-fit:contain}.image-footer{display:flex;justify-content:space-between;gap:12px;margin-top:7px;font-size:11px;color:#74818d;font-variant-numeric:tabular-nums}.image-footer a{text-decoration:none;color:var(--muted)}.image-footer a:hover{text-decoration:underline;color:var(--blue)}.render-error{min-height:240px;aspect-ratio:6/5;border:1px dashed #d0a7ae;border-radius:5px;background:#fff7f8;display:flex;flex-direction:column;justify-content:center;align-items:center;padding:24px;text-align:center;color:#873b49}.render-error p{font-size:13px;max-width:440px;overflow-wrap:anywhere}
.source-details{border-top:1px solid var(--line);font-size:13px}.source-details summary{padding:12px 24px;cursor:pointer;color:var(--muted);background:#fafbfc}.source-details[open] summary{border-bottom:1px solid var(--line)}.details-content{padding:18px 24px 22px}.details-content dl{margin:0}.details-content dl>div{display:grid;grid-template-columns:140px 1fr;gap:10px;margin-bottom:8px}.details-content dt{color:var(--muted)}.details-content dd{margin:0;min-width:0;overflow-wrap:anywhere}.details-content code{font-size:12px}.date-note{color:var(--muted);font-size:12px;margin:14px 0}.records-link{display:inline-block;margin:0 0 4px}.details-content h4{margin:20px 0 8px;font-size:13px}.details-content pre{margin:0;padding:16px;background:#f3f5f7;border-radius:5px;font:12px/1.8 ui-monospace,"Noto Sans CJK JP",monospace;white-space:pre-wrap;overflow-wrap:anywhere}.empty{text-align:center;padding:65px 20px;border:1px dashed #c7d1da;border-radius:10px;background:#ffffff80}.empty h2{font-size:21px;margin:0 0 8px}.empty p{font-size:14px;color:var(--muted);margin:0 0 16px}.page-footer{padding-top:0;padding-bottom:30px;display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:12px}.page-footer p{margin:0}[hidden]{display:none!important}
.build-info{margin:8px 0 0;font-size:11px;color:var(--muted);overflow-wrap:anywhere}.filters .reason-filter{grid-column:1/-1}.reason-summary{margin-top:14px;font-size:12px;color:var(--muted)}.reason-summary summary{cursor:pointer}.reason-summary p{margin:7px 0}.reason-summary ul{display:flex;flex-wrap:wrap;gap:6px 18px;padding:0;margin:10px 0 0;list-style:none}.reason-summary li{overflow-wrap:anywhere}.reason-summary strong{color:var(--ink);font-variant-numeric:tabular-nums}.comparison-grid.single{grid-template-columns:minmax(0,1fr)}.display-decision{margin-top:12px;padding:10px 12px;background:#f5f7f9;border:1px solid var(--line);border-radius:5px;font-size:12px;overflow-wrap:anywhere}.display-decision p{margin:0}.decision-heading{font-weight:650}.display-decision ul{padding-left:18px;margin:6px 0 0}.display-decision li+li{margin-top:7px}.reason-stage{color:var(--muted);font-size:11px;border:1px solid var(--line);border-radius:3px;padding:1px 4px}.display-decision .reason-detail{margin-top:3px;color:var(--muted);white-space:pre-wrap}.display-decision .decision-unchanged{margin-top:4px;color:var(--muted)}.display-decision.unrecorded{padding:6px 0;border:0;background:none;color:var(--muted)}
.view-controls{display:flex;align-items:center;flex-wrap:wrap;gap:10px 20px;padding-bottom:16px;margin-bottom:16px;border-bottom:1px solid var(--line)}.view-controls label{display:flex;align-items:center;flex-wrap:wrap;gap:8px;font-size:12px;font-weight:650;color:var(--muted)}.view-controls select{padding:8px 10px;background:#fff;border:1px solid #cbd4dc;color:var(--ink);border-radius:6px;font-size:14px;line-height:1.5}.view-note{flex:1 1 260px;margin:0;color:var(--muted);font-size:12px}.image-viewport{min-width:0}body[data-image-width] .image-viewport{overflow-x:auto;overscroll-behavior-x:contain;padding-bottom:6px}body[data-image-width] .image-link{width:var(--image-width);margin-inline:auto}body[data-image-width] .image-heading,body[data-image-width] .image-footer,body[data-image-width] .display-decision{max-width:var(--image-width);margin-left:auto;margin-right:auto}.image-viewport:focus-visible{outline:3px solid #3c87bd;outline-offset:2px}
@media(min-width:1700px){.header-inner,main,.page-footer{max-width:1640px}}
@media(max-width:1000px){.header-inner,main,.page-footer{padding-left:24px;padding-right:24px}.filters{grid-template-columns:repeat(4,1fr)}.filters .search{grid-column:1/-1}.case-heading{padding:20px 20px 0}.case-labels{padding-left:20px;padding-right:20px}.description{margin-left:20px;margin-right:20px}.comparison-grid{padding-left:20px;padding-right:20px;gap:14px}.case h2{font-size:21px}}
@media(max-width:680px){.header-inner,main,.page-footer{padding-left:16px;padding-right:16px}.header-inner{padding-top:24px;padding-bottom:21px}.heading-line{align-items:flex-start}.heading-line h1{font-size:26px}.heading-line a{font-size:11px;padding-top:7px}.intro{font-size:13px}.notice{font-size:11px;align-items:flex-start;gap:7px}.notice::before{margin-top:6px}.toolbar{padding:15px}.filters{grid-template-columns:1fr 1fr;gap:10px}.toolbar-top{align-items:flex-start}.toolbar-top p{font-size:12px}.toolbar-top strong{font-size:16px}.case-heading{padding:17px 16px 0}.case h2{font-size:20px}.case-labels{padding-left:16px;padding-right:16px;gap:5px;font-size:11px}.record-count{margin-left:0}.description{margin:10px 16px 16px;font-size:12px}.comparison-grid{grid-template-columns:1fr;padding:0 16px 17px;gap:19px}.source-details summary{padding:11px 16px}.details-content{padding:16px}.details-content dl>div{grid-template-columns:1fr;gap:1px;margin-bottom:11px}.page-footer{display:block}.page-footer p+p{margin-top:7px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
'''


_CSS += '''
body[data-view-mode="posts"] .case[data-has-posts="true"] .image-only,body[data-view-mode="images"] .post-view,body[data-view-mode="images"] .post-unavailable{display:none}
.post-view{padding:0 24px 24px}.post-unavailable,.post-empty{margin:0 24px 20px;color:var(--muted);font-size:13px}.post-empty{margin:0;padding:24px;border:1px dashed var(--line);border-radius:8px;text-align:center}.post-unit+.post-unit{border-top:1px solid var(--line);padding-top:22px;margin-top:26px}.post-unit-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:14px}.post-unit-heading h3{font-size:15px;margin:0}.post-unit-heading p{margin:0;color:var(--muted);font-size:12px;overflow-wrap:anywhere}.post-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px}.post-grid.single{grid-template-columns:minmax(0,1fr)}.post-comparison{min-width:0}.post-comparison h4{margin:0;font-size:13px}.post-viewport{min-width:0}.post-viewport:focus-visible{outline:3px solid #3c87bd;outline-offset:2px}.post-frame{width:100%;padding:16px;border:1px solid #cbd5df;border-radius:10px;background:#fff;overflow:hidden}.post-body{white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px;line-height:1.7}.post-attachment{display:inline-grid;max-width:100%;place-items:center;vertical-align:middle;line-height:0}.post-attachment img{display:block;width:auto;height:auto;max-width:calc(100% - 1.5em);max-height:20em;margin:.5em;border:1px solid lightgray;border-radius:5px}.post-attachment-error{display:inline-block;width:100%;vertical-align:top;white-space:normal;margin:0;padding:20px;background:#fff7f8;border-top:1px dashed #d0a7ae;color:#873b49;font-size:13px;overflow-wrap:anywhere}.post-attachment-error-detail{display:block;margin-top:8px}.post-note{font-size:12px;color:var(--muted);margin:0 0 16px}.reason-scope{font-size:12px;color:var(--muted);margin:12px 0 0}
.post-fold{position:relative}.post-fold[data-folded="true"] .post-clip{max-height:30rem;overflow:hidden}.post-toggle{display:block;width:100%;padding:.5rem;border:0;background:#fff;color:var(--blue)}.post-fold[data-folded="true"] .post-toggle{position:absolute;bottom:0;left:0;padding:2.2rem .5rem .5rem;background:linear-gradient(transparent,#fff 55%)}
body[data-image-width] .post-viewport{overflow-x:auto;overscroll-behavior-x:contain;padding-bottom:6px}body[data-image-width] .post-frame{width:var(--image-width);margin-inline:auto}
@media(max-width:1000px){.post-view{padding-left:20px;padding-right:20px}.post-grid{gap:14px}}
@media(max-width:680px){.post-view{padding:0 16px 18px}.post-grid{grid-template-columns:minmax(0,1fr);gap:20px}.post-unit-heading{align-items:flex-start;flex-direction:column;gap:3px}.post-note{font-size:11px}.post-unavailable{margin-left:16px;margin-right:16px}}
'''


_CSS += '''
.image-alt{margin-top:10px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}.image-alt summary{cursor:pointer}.image-alt-text{white-space:pre-wrap;padding:10px 12px;margin-top:6px;background:#f5f7f9;border:1px solid var(--line);border-radius:5px}.image-alt p{margin:7px 0 0}.alt-recording{font-size:11px;margin-left:8px;color:#74818d}body[data-image-width] .image-alt{max-width:var(--image-width);margin-left:auto;margin-right:auto}
.baseline-info{padding:12px 14px;background:#f3f6f9;border:1px solid var(--line);border-radius:6px;margin-top:14px;font-size:12px;color:var(--muted)}.baseline-info p{margin:0}.baseline-info p+p{margin-top:5px}.baseline-info a{margin-left:10px}.baseline-unmatched{margin-top:8px}.baseline-unmatched summary{cursor:pointer}.baseline-unmatched ul{margin:7px 0 0;padding-left:20px}.baseline-unmatched code{font-size:11px;margin-left:6px}.comparison-notice{margin:0 24px 18px;padding:12px 14px;background:#f5f7f9;border:1px solid var(--line);border-radius:6px;font-size:13px}.comparison-notice>div{display:flex;align-items:center;flex-wrap:wrap;gap:6px}.comparison-notice p{margin:7px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--muted);font-size:12px}.comparison-status{font-weight:650;padding:2px 8px;border-radius:4px;border:1px solid #d0dbe4;background:#edf2f6;color:#415568}.comparison-status.changed{background:#fff6e8;color:#80581e;border-color:#e9d5b5}.comparison-status.input_changed{background:#f3eefb;color:#665085;border-color:#ded4ec}.comparison-status.added{background:#edf6f1;color:#276857;border-color:#d0e4d9}.comparison-change{padding:1px 7px;border:1px solid var(--line);border-radius:4px;background:#fff;font-size:11px}.baseline-post-view>.comparison-notice{margin-left:0;margin-right:0}.baseline-post-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px;align-items:start}.baseline-column{min-width:0}.baseline-column-heading{font-size:15px;margin:0 0 16px;padding-bottom:9px;border-bottom:2px solid var(--line)}.previous>.baseline-column-heading,.previous .image-heading h3{color:var(--blue)}.current>.baseline-column-heading,.current .image-heading h3{color:var(--preview)}.baseline-unavailable{padding:24px;margin:0;border:1px dashed var(--line);border-radius:6px;color:var(--muted);font-size:13px}.filters .comparison-filter{grid-column:1/-1}.comparison-counts{margin-top:14px;font-size:12px;color:var(--muted)}.comparison-counts p{margin:0 0 6px}.comparison-counts ul{display:flex;gap:6px 18px;flex-wrap:wrap;list-style:none;padding:0;margin:0}.comparison-counts strong{color:var(--ink)}
@media(max-width:1000px){.comparison-notice{margin-left:20px;margin-right:20px}.baseline-post-grid{gap:14px}}
@media(max-width:680px){.comparison-notice{margin-left:16px;margin-right:16px}.baseline-post-grid{grid-template-columns:minmax(0,1fr);gap:24px}.baseline-info a{display:block;margin:4px 0 0}}
'''


_JS = '''
(() => {
  const form = document.getElementById('filters');
  const cards = Array.from(document.querySelectorAll('.case'));
  const query = document.getElementById('search');
  const reason = document.getElementById('reason');
  const comparisonStatus = document.getElementById('comparison-status');
  const imageWidth = document.getElementById('image-width');
  const viewMode = document.getElementById('view-mode');
  const viewports = Array.from(document.querySelectorAll('.image-viewport, .post-viewport'));
  const selectors = ['category', 'source-kind', 'classification', 'state'];
  const normalize = text => text.normalize('NFKC').toLocaleLowerCase('ja');
  const searchable = cards.map(card => normalize(card.textContent));
  const postFolds = Array.from(document.querySelectorAll('.post-fold')).map(element => ({
    element, content: element.querySelector('.post-body'),
    button: element.querySelector('.post-toggle'), folded: true
  }));
  let layoutFrame = null;
  function updatePostFolds() {
    // Match the CSS 30rem limit, including a browser's root font-size setting.
    const maxHeight = 30 * parseFloat(getComputedStyle(document.documentElement).fontSize);
    postFolds.forEach(fold => {
      if (!fold.content.getClientRects().length) return;
      const overflowing = fold.content.getBoundingClientRect().height > maxHeight + 1;
      fold.element.dataset.folded = String(overflowing && fold.folded);
      fold.button.hidden = !overflowing;
      fold.button.setAttribute('aria-expanded', String(!overflowing || !fold.folded));
      fold.button.textContent = fold.folded ? 'もっと見る' : '元に戻す';
    });
  }
  function scheduleLayout() {
    if (layoutFrame !== null) return;
    layoutFrame = requestAnimationFrame(() => {
      layoutFrame = null;
      updatePostFolds();
      updateScrollFocus();
    });
  }
  postFolds.forEach(fold => {
    fold.button.addEventListener('click', () => {
      fold.folded = !fold.folded;
      updatePostFolds();
      if (fold.folded) fold.element.scrollIntoView({
        block: 'nearest',
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
      });
    });
    fold.content.querySelectorAll('img').forEach(image => {
      image.addEventListener('load', scheduleLayout);
      image.addEventListener('error', scheduleLayout);
    });
  });
  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(scheduleLayout);
    postFolds.forEach(fold => observer.observe(fold.content));
  }
  if (document.fonts) document.fonts.ready.then(scheduleLayout);
  function updateScrollFocus() {
    viewports.forEach(viewport => {
      if (viewport.clientWidth && viewport.scrollWidth > viewport.clientWidth + 1) {
        viewport.tabIndex = 0;
      } else {
        viewport.removeAttribute('tabindex');
      }
    });
  }
  function updateImageWidth() {
    const width = ['320', '360', '430'].includes(imageWidth.value) ? imageWidth.value : '';
    if (width) {
      document.body.dataset.imageWidth = width;
      document.body.style.setProperty('--image-width', width + 'px');
    } else {
      delete document.body.dataset.imageWidth;
      document.body.style.removeProperty('--image-width');
    }
    document.getElementById('image-size-note').textContent = width
      ? '画像・投稿枠を幅 ' + width + 'pxで表示。画面に収まらない部分は横にスクロールできます。'
      : '通常表示：画像・投稿枠を欄の幅に合わせます。画像を選ぶと原寸で開きます。';
    viewports.forEach(viewport => { viewport.scrollLeft = 0; });
    scheduleLayout();
  }
  function update() {
    const postMode = document.body.dataset.viewMode === 'posts';
    const reasonCodes = cards.map(card => JSON.parse(postMode ? card.dataset.postReasonCodes : card.dataset.reasonCodes));
    const comparisonCodes = cards.map(card => postMode ? card.dataset.postComparisonStatus : card.dataset.comparisonStatus);
    const terms = normalize(query.value).trim().split(/\\s+/).filter(Boolean);
    const counts = new Map();
    const comparisonCounts = new Map();
    let shown = 0;
    cards.forEach((card, index) => {
      const selected = selectors.every(key => {
        const value = document.getElementById(key).value;
        return !value || card.getAttribute('data-' + key) === value;
      });
      const matches = selected && terms.every(term => searchable[index].includes(term));
      const matchesReason = !reason.value || reasonCodes[index].includes(reason.value);
      const matchesComparison = !comparisonStatus || !comparisonStatus.value || comparisonStatus.value === comparisonCodes[index];
      if (matches && matchesComparison) reasonCodes[index].forEach(code => counts.set(code, (counts.get(code) || 0) + 1));
      if (matches && matchesReason && comparisonStatus) {
        const code = comparisonCodes[index];
        comparisonCounts.set(code, (comparisonCounts.get(code) || 0) + 1);
      }
      const visible = matches && matchesReason && matchesComparison;
      card.hidden = !visible;
      if (visible) shown++;
    });
    document.getElementById('shown-count').textContent = String(shown);
    document.getElementById('empty').hidden = shown !== 0;
    Array.from(reason.options).filter(option => option.value).forEach(option => {
      option.textContent = option.dataset.label + '（' + (counts.get(option.value) || 0) + '件）';
    });
    document.querySelectorAll('[data-reason-count]').forEach(item => {
      item.textContent = String(counts.get(item.dataset.reasonCount) || 0);
    });
    if (comparisonStatus) Array.from(comparisonStatus.options).filter(option => option.value).forEach(option => {
      option.textContent = option.dataset.label + '（' + (comparisonCounts.get(option.value) || 0) + '件）';
    });
    document.querySelectorAll('[data-comparison-count]').forEach(item => {
      item.textContent = String(comparisonCounts.get(item.dataset.comparisonCount) || 0);
    });
    scheduleLayout();
  }
  function reset() {
    form.reset();
    update();
    query.focus();
  }
  function updateViewMode() {
    const postMode = viewMode.value === 'posts';
    document.body.dataset.viewMode = postMode ? 'posts' : 'images';
    const versionLabel = document.body.dataset.baseline ? '今回の作業版' : '作業版';
    document.getElementById('reason-scope').textContent = postMode
      ? '集計対象：投稿全体の添付画像（' + versionLabel + '）。同じケースの複数投稿は、理由ごとに1件として数えます。未記録のケースは画像のみの理由を表示します。'
      : '集計対象：画像のみの表示（' + versionLabel + '）。投稿全体の添付画像とは異なる場合があります。';
    if (comparisonStatus) document.getElementById('comparison-scope').textContent =
      '前回との比較：' + (postMode ? '投稿全体' : '画像のみ') + '。比較状態以外の検索条件を適用した件数です。';
    update();
  }
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  form.addEventListener('submit', event => event.preventDefault());
  document.querySelectorAll('[data-reset]').forEach(button => button.addEventListener('click', reset));
  imageWidth.addEventListener('change', updateImageWidth);
  viewMode.addEventListener('change', updateViewMode);
  window.addEventListener('resize', scheduleLayout);
  document.getElementById('view-controls').hidden = false;
  form.hidden = false;
  document.getElementById('reset').hidden = false;
  updateImageWidth();
  updateViewMode();
})();
'''


def render_html(manifest):
    """Render complete escaped HTML; the browser never fetches its manifest."""
    cases = manifest['cases']
    baseline = manifest.get('baseline')
    if baseline is not None and not isinstance(baseline, dict):
        raise ValueError('Gallery baseline must be an object')
    has_baseline = baseline is not None
    if has_baseline:
        for case in cases:
            for mode in ('images', 'posts'):
                _comparison_result(case, mode)
            previous = case.get('comparison', {}).get('previous')
            if previous is not None and not isinstance(previous, dict):
                raise ValueError('Previous gallery case must be an object or null')
    has_post_data = any('posts' in case or (has_baseline and
                        'posts' in (case.get('comparison', {}).get('previous') or {})) for case in cases)
    default_mode = 'posts' if has_post_data else 'images'
    published_version = manifest.get('published_version', '公開済み版')
    comparison = any('published' in case['images'] for case in cases)
    intro = (f'同じ入力データで、公開済み {_escape(published_version)} と作業版を比較できます。'
             if comparison else '作業版の画像と、受信内容に応じた表示理由を確認できます。')
    if has_post_data:
        intro = ('投稿本文と添付画像を、投稿単位で確認できます。'
                 '「画像のみ」に切り替えると画像と表示理由を確認できます。')
        if comparison:
            intro += f' 公開済み {_escape(published_version)} と作業版も比較できます。'
    if has_baseline:
        intro = ('前回の作業版と今回の作業版を比較できます。'
                 '「画像のみ」と「投稿全体」で、比較する内容を切り替えられます。'
                 if has_post_data else '前回の作業版と今回の作業版の画像を比較できます。')
    baseline_info = ''
    if has_baseline:
        path = _local_path(baseline['manifest_path'])
        working_tree = (' · 未コミットの変更を含む' if baseline.get('working_tree') is True
                        else ' · 未コミットの変更なし' if baseline.get('working_tree') is False else '')
        baseline_info = ('<div class="baseline-info"><p>前回のコード：'
                         f'<code>{_escape(baseline.get("commit") or "記録なし")}</code>{working_tree}'
                         f'<a href="{path}" target="_blank" rel="noopener">前回の一覧データ JSON ↗</a></p>'
                         '<p>同じ入力での変化を「変更あり」として表示します。入力が異なるケースは別に示します。</p>')
        unmatched = baseline.get('unmatched_cases', [])
        if unmatched:
            entries = ''.join(f'<li>{_escape(case["title"])}<code>{_escape(case["slug"])}</code></li>'
                              for case in unmatched)
            baseline_info += ('<details class="baseline-unmatched"><summary>'
                              f'今回の比較対象にない前回ケース（{len(unmatched)}件）</summary>'
                              '<p>今回の選択範囲に含まれない前回ケースです。</p>'
                              f'<ul>{entries}</ul></details>')
        baseline_info += '</div>'
    build_info = ''
    if manifest.get('commit'):
        working_tree = (' · 未コミットの変更を含む' if manifest.get('working_tree') is True
                        else ' · 未コミットの変更なし' if manifest.get('working_tree') is False else '')
        build_info = (f'<p class="build-info">{"今回のコード" if has_baseline else "コード"}：'
                      f'<code>{_escape(manifest["commit"])}</code>'
                      f'{working_tree}</p>')
    slugs = [case['slug'] for case in cases]
    if len(set(slugs)) != len(slugs):
        raise ValueError('Gallery case IDs must be unique')
    rows = '\n'.join(_case(case, index, published_version, baseline=has_baseline)
                     for index, case in enumerate(cases, 1))
    filters = ''.join(
        f'<label>{label}<select id="{element}" name="{element}"><option value="">すべて</option>'
        f'{_options((case[key] for case in cases), labels)}</select></label>'
        for key, element, label, labels in (
            ('category', 'category', '情報の種類', CATEGORIES),
            ('source_kind', 'source-kind', 'データの由来', SOURCES),
            ('classification', 'classification', '電文の区分', CLASSIFICATIONS),
            ('state', 'state', '状態', STATES)))
    reason_labels, reason_counts = {}, {}
    for case in cases:
        image_items = dict(_reason_items(case['images'].get('preview', {})))
        post_items = dict(_post_reason_items(case))
        for items in (image_items, post_items):
            for code, label in items.items():
                reason_labels.setdefault(code, label)
                reason_counts.setdefault(code, 0)
        for code in (post_items if has_post_data else image_items):
            reason_counts[code] += 1
    reason_options = ''.join(
        f'<option value="{_escape(code)}" data-label="{_escape(label)}">'
        f'{_escape(label)}（{reason_counts[code]}件）</option>'
        for code, label in reason_labels.items())
    version_label = '今回の作業版' if has_baseline else '作業版'
    filters += (f'<label class="reason-filter">表示理由・記録状態（{version_label}）'
                '<select id="reason" name="reason"><option value="">すべて</option>'
                f'{reason_options}</select></label>')
    reason_totals = ''.join(
        f'<li>{_escape(label)}：<strong data-reason-count="{_escape(code)}">'
        f'{reason_counts[code]}</strong> 件</li>' for code, label in reason_labels.items())
    reason_summary = (f'<details class="reason-summary"><summary>{version_label}の理由別件数</summary>'
                      '<p>理由以外の検索条件を適用した件数です。複数の理由を持つ画像は各理由に数えます。'
                      '描画エラーは表示の切替と分けて集計します。</p>'
                      f'<ul>{reason_totals}</ul></details>')
    comparison_summary = ''
    if has_baseline:
        mode = 'posts' if has_post_data else 'images'
        comparison_counts = {status: sum(_comparison_result(case, mode)['status'] == status for case in cases)
                             for status in COMPARISON_STATUSES}
        options = ''.join(f'<option value="{status}" data-label="{label}">'
                          f'{label}（{comparison_counts[status]}件）</option>'
                          for status, label in COMPARISON_STATUSES.items())
        filters += ('<label class="comparison-filter">前回との比較'
                    '<select id="comparison-status" name="comparison-status"><option value="">すべて</option>'
                    f'{options}</select></label>')
        totals = ''.join(f'<li>{label}：<strong data-comparison-count="{status}">'
                         f'{comparison_counts[status]}</strong> 件</li>'
                         for status, label in COMPARISON_STATUSES.items())
        comparison_summary = ('<div class="comparison-counts"><p id="comparison-scope">前回との比較：'
                              f'{"投稿全体" if has_post_data else "画像のみ"}。比較状態以外の検索条件を適用した件数です。'
                              f'</p><ul>{totals}</ul></div>')
    mode_options = ('<label for="view-mode">表示内容<select id="view-mode">'
                    f'<option value="images"{" selected" if not has_post_data else ""}>画像のみ</option>'
                    f'<option value="posts"{" selected" if has_post_data else " disabled"}>投稿全体</option>'
                    '</select></label>')
    post_note = ('<p class="post-note">画像有効・履歴なしで、入力が待機時間内に揃った場合の'
                 'オフライン投稿例です。添付画像はローカルPNGで表示しています。'
                 '本文と画像はnostterの通常TL相当で表示し、長い投稿は「もっと見る」で全体を開けます。</p>'
                 if has_post_data else '')
    scope = f'{"投稿全体の添付画像" if has_post_data else "画像のみの表示"}（{version_label}）'
    scope_note = f'<p id="reason-scope" class="reason-scope">集計対象：{scope}</p>'
    categories = len({case['category'] for case in cases})
    license_link = (f'<br><a href="{_local_path(manifest["source_license_path"])}" '
                    'target="_blank" rel="noopener">受信ログのライセンス</a>'
                    if manifest.get('source_license_path') else '')
    return f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>画像ギャラリー | Shirasu</title>
<style>{_CSS}</style></head><body data-view-mode="{default_mode}"{' data-baseline="true"' if has_baseline else ''}>
<header class="page-header"><div class="header-inner"><div class="heading-line"><h1>画像ギャラリー</h1>
<a href="manifest.json" target="_blank" rel="noopener">一覧データ JSON ↗</a></div>
<p class="intro">{intro}</p>{build_info}{baseline_info}
<p class="notice">過去の受信・訓練・合成サンプル / 現在の災害情報ではありません</p></div></header>
<main><section class="toolbar" aria-label="表示するサンプルを選択"><div class="toolbar-top">
<p role="status" aria-live="polite" aria-atomic="true"><strong id="shown-count">{len(cases)}</strong> / {len(cases)} 件を表示 <span>· {categories} 種類</span></p>
<button id="reset" type="button" class="reset" data-reset hidden>条件をリセット</button></div>
<div id="view-controls" class="view-controls" hidden>{mode_options}<label for="image-width">表示サイズ
<select id="image-width" aria-describedby="image-size-note">
<option value="normal">通常（欄に合わせる）</option>
<option value="320">SNS · 320px</option>
<option value="360" selected>SNS · 360px（基準）</option>
<option value="430">SNS · 430px</option>
</select></label><p id="image-size-note" class="view-note" role="status" aria-live="polite"></p></div>{post_note}
<form id="filters" class="filters" hidden><label class="search">キーワード検索<input id="search" name="search" type="search" placeholder="例：台風、阿蘇山、解除" autocomplete="off"></label>{filters}</form>{scope_note}{reason_summary}{comparison_summary}
</section><noscript><p class="no-script">すべてのサンプルを表示しています。絞り込みにはJavaScriptが必要です。</p></noscript>
<div id="cases">{rows}</div>
<section id="empty" class="empty" hidden><h2>該当するサンプルがありません</h2><p>検索語や絞り込み条件を変更してください。</p><button type="button" class="reset" data-reset>条件をリセット</button></section>
</main><footer class="page-footer"><p>Shirasu · オフライン画像レビュー<br>南海トラフは画像を添付しない投稿です。</p><p>画像を選ぶと原寸で開きます。出典と入力電文は各サンプルから確認できます。{license_link}</p></footer>
<script>{_JS}</script></body></html>
'''
