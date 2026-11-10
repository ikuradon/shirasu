"""Self-contained, file:// compatible HTML for the offline image review tool."""

from html import escape
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit


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
KINDS = {'map': '地図', 'summary': '要旨', 'text': 'テキスト'}


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


def _image(case, side, published_version):
    value = case['images'][side]
    heading = '公開版' if side == 'published' else '作業版'
    version = value.get('version', published_version if side == 'published' else 'preview')
    kind = KINDS.get(value.get('kind'), '描画')
    top = (f'<div class="image-heading"><h3>{heading} '
           f'<span class="version">{_escape(version)}</span></h3>'
           f'<span class="kind">{_escape(kind)}</span></div>')
    if value.get('error'):
        content = ('<div class="render-error"><strong>描画できませんでした</strong>'
                   f'<p>{_escape(value["error"])}</p></div>')
        return f'<section class="comparison {side}">{top}{content}</section>'
    path = _local_path(value['path'])
    width, height = value.get('width', 1440), value.get('height', 1200)
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError('Gallery image dimensions must be positive integers')
    label = _escape(f'{case["title"]} / {heading} / {kind}')
    content = (f'<a class="image-link" href="{path}" target="_blank" rel="noopener" '
               f'aria-label="{label}を原寸で開く">'
               f'<img src="{path}" width="{width}" height="{height}" '
               f'alt="{label}" loading="lazy" decoding="async"></a>'
               f'<div class="image-footer"><span>{width} × {height}</span>'
               f'<a href="{path}" target="_blank" rel="noopener">PNGを原寸で開く ↗</a></div>')
    return f'<section class="comparison {side}">{top}{content}</section>'


def _case(case, index, published_version):
    slug = case['slug']
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', slug):
        raise ValueError('Gallery case IDs must be lowercase slug names')
    category = CATEGORIES.get(case['category'], case['category'])
    source = SOURCES.get(case['source_kind'], case['source_kind'])
    classification = CLASSIFICATIONS.get(case['classification'], case['classification'])
    state = STATES.get(case['state'], case['state'])
    tags = ''.join(f'<span class="tag">{_escape(tag)}</span>' for tag in case.get('tags', ()))
    attributes = ' '.join(f'data-{key.replace("_", "-")}="{_escape(case[key])}"'
                          for key in ('category', 'source_kind', 'classification', 'state'))
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
    paths = _local_path(case['records_path'])
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
  <div class="comparison-grid">{_image(case, 'published', published_version)}
    {_image(case, 'preview', published_version)}</div>
  <details class="source-details"><summary>出典・入力データを確認</summary>
    <div class="details-content"><dl>{metadata}</dl>
    <p class="date-note">基準時刻は電文の日付を復元するために使用します。このギャラリーの公開受信ログには、電文ごとの正確な受信時刻は記録されていません。</p>
    <a class="records-link" href="{paths}" target="_blank" rel="noopener">入力JSONを開く ↗</a>{body}</div>
  </details>
</article>'''


_CSS = '''
:root{color-scheme:light;--bg:#f3f5f7;--paper:#fff;--ink:#17232f;--muted:#576574;--line:#dce2e8;--blue:#1d608e;--preview:#64538d}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:1rem}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 system-ui,-apple-system,"Noto Sans CJK JP","Yu Gothic",sans-serif}
a{color:var(--blue);text-underline-offset:3px}button,input,select{font:inherit}button,select{cursor:pointer}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #3c87bd;outline-offset:4px}
.page-header{background:var(--paper);border-bottom:1px solid var(--line)}.header-inner,main,.page-footer{max-width:1520px;margin:auto;padding-left:40px;padding-right:40px}.header-inner{padding-top:34px;padding-bottom:25px}.heading-line{display:flex;justify-content:space-between;align-items:center;gap:20px}.heading-line h1{font-size:31px;letter-spacing:.03em;margin:0;line-height:1.4}.heading-line a{font-size:13px;text-decoration:none}.intro{margin:9px 0 0;color:var(--muted)}.notice{display:flex;align-items:center;gap:10px;font-size:13px;color:#66552d;margin:18px 0 0}.notice::before{content:"";width:7px;height:7px;border-radius:50%;background:#ae8a40;flex:none}
main{padding-top:24px;padding-bottom:50px}.toolbar{padding:20px;background:var(--paper);border:1px solid var(--line);border-radius:10px;margin-bottom:24px}.toolbar-top{display:flex;align-items:center;gap:16px;justify-content:space-between;margin-bottom:13px}.toolbar-top p{margin:0;color:var(--muted);font-size:13px}.toolbar-top strong{font-size:18px;color:var(--ink);font-variant-numeric:tabular-nums}.reset{border:0;background:none;color:var(--blue);padding:3px 0;text-decoration:underline;text-underline-offset:3px;font-size:13px}.filters{display:grid;grid-template-columns:minmax(210px,1.5fr) repeat(4,minmax(125px,1fr));gap:12px}.filters label{display:flex;flex-direction:column;gap:5px;font-size:12px;font-weight:650;color:var(--muted)}.filters input,.filters select{min-width:0;width:100%;padding:9px 11px;background:#fff;border:1px solid #cbd4dc;color:var(--ink);border-radius:6px;font-size:14px;line-height:1.5}.filters input::placeholder{color:#7d8a96;font-weight:400}.no-script{margin:0 0 20px;color:var(--muted)}
.case{background:var(--paper);border:1px solid var(--line);border-radius:12px;margin-bottom:26px;overflow:hidden;scroll-margin-top:20px}.case-heading{display:flex;justify-content:space-between;align-items:center;padding:24px 24px 0;gap:16px}.category{margin:0 0 3px;font-size:12px;color:var(--muted);font-weight:650;letter-spacing:.03em}.case-number{color:#8392a1;font-weight:400;font-variant-numeric:tabular-nums;margin-right:7px}.case h2{margin:0;font-size:23px;line-height:1.5;letter-spacing:.015em}.permalink{font-size:21px;color:#8795a3;text-decoration:none;padding:5px}.case-labels{display:flex;align-items:center;flex-wrap:wrap;gap:7px;padding:12px 24px 0;font-size:12px}.source-label,.classification,.state,.tag{display:inline-block;border:1px solid var(--line);padding:2px 8px;border-radius:4px;background:#fafbfc;line-height:1.7}.source-label{color:#415568}.classification.training{background:#fff8e9;border-color:#eadab6;color:#72541c}.classification.ordinary{background:#eff5f9;border-color:#d4e1eb;color:#345e7d}.state.release{color:#276857;background:#edf6f1;border-color:#d0e4d9}.state.cancel{color:#8a3946;background:#fcf0f2;border-color:#e9cdd3}.tag{color:var(--muted);border-color:transparent;background:#f0f3f6}.record-count{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}.description{margin:11px 24px 19px;color:var(--muted);font-size:14px;max-width:1100px}.comparison-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:0 24px 21px}.comparison{min-width:0}.image-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:10px}.image-heading h3{font-size:13px;margin:0;font-weight:700}.published .image-heading h3{color:var(--blue)}.preview .image-heading h3{color:var(--preview)}.version{font-size:11px;font-weight:500;padding-left:6px;color:var(--muted)}.kind{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:3px;padding:1px 6px}.image-link{display:block;background:#10171e;border-radius:5px;overflow:hidden;line-height:0;box-shadow:0 0 0 1px #17232f10}.image-link img{display:block;width:100%;height:auto;object-fit:contain}.image-footer{display:flex;justify-content:space-between;gap:12px;margin-top:7px;font-size:11px;color:#74818d;font-variant-numeric:tabular-nums}.image-footer a{text-decoration:none;color:var(--muted)}.image-footer a:hover{text-decoration:underline;color:var(--blue)}.render-error{min-height:240px;aspect-ratio:6/5;border:1px dashed #d0a7ae;border-radius:5px;background:#fff7f8;display:flex;flex-direction:column;justify-content:center;align-items:center;padding:24px;text-align:center;color:#873b49}.render-error p{font-size:13px;max-width:440px;overflow-wrap:anywhere}
.source-details{border-top:1px solid var(--line);font-size:13px}.source-details summary{padding:12px 24px;cursor:pointer;color:var(--muted);background:#fafbfc}.source-details[open] summary{border-bottom:1px solid var(--line)}.details-content{padding:18px 24px 22px}.details-content dl{margin:0}.details-content dl>div{display:grid;grid-template-columns:140px 1fr;gap:10px;margin-bottom:8px}.details-content dt{color:var(--muted)}.details-content dd{margin:0;min-width:0;overflow-wrap:anywhere}.details-content code{font-size:12px}.date-note{color:var(--muted);font-size:12px;margin:14px 0}.records-link{display:inline-block;margin:0 0 4px}.details-content h4{margin:20px 0 8px;font-size:13px}.details-content pre{margin:0;padding:16px;background:#f3f5f7;border-radius:5px;font:12px/1.8 ui-monospace,"Noto Sans CJK JP",monospace;white-space:pre-wrap;overflow-wrap:anywhere}.empty{text-align:center;padding:65px 20px;border:1px dashed #c7d1da;border-radius:10px;background:#ffffff80}.empty h2{font-size:21px;margin:0 0 8px}.empty p{font-size:14px;color:var(--muted);margin:0 0 16px}.page-footer{padding-top:0;padding-bottom:30px;display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:12px}.page-footer p{margin:0}[hidden]{display:none!important}
@media(min-width:1700px){.header-inner,main,.page-footer{max-width:1640px}}
@media(max-width:1000px){.header-inner,main,.page-footer{padding-left:24px;padding-right:24px}.filters{grid-template-columns:repeat(4,1fr)}.filters .search{grid-column:1/-1}.case-heading{padding:20px 20px 0}.case-labels{padding-left:20px;padding-right:20px}.description{margin-left:20px;margin-right:20px}.comparison-grid{padding-left:20px;padding-right:20px;gap:14px}.case h2{font-size:21px}}
@media(max-width:680px){.header-inner,main,.page-footer{padding-left:16px;padding-right:16px}.header-inner{padding-top:24px;padding-bottom:21px}.heading-line{align-items:flex-start}.heading-line h1{font-size:26px}.heading-line a{font-size:11px;padding-top:7px}.intro{font-size:13px}.notice{font-size:11px;align-items:flex-start;gap:7px}.notice::before{margin-top:6px}.toolbar{padding:15px}.filters{grid-template-columns:1fr 1fr;gap:10px}.toolbar-top{align-items:flex-start}.toolbar-top p{font-size:12px}.toolbar-top strong{font-size:16px}.case-heading{padding:17px 16px 0}.case h2{font-size:20px}.case-labels{padding-left:16px;padding-right:16px;gap:5px;font-size:11px}.record-count{margin-left:0}.description{margin:10px 16px 16px;font-size:12px}.comparison-grid{grid-template-columns:1fr;padding:0 16px 17px;gap:19px}.source-details summary{padding:11px 16px}.details-content{padding:16px}.details-content dl>div{grid-template-columns:1fr;gap:1px;margin-bottom:11px}.page-footer{display:block}.page-footer p+p{margin-top:7px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
'''


_JS = '''
(() => {
  const form = document.getElementById('filters');
  const cards = Array.from(document.querySelectorAll('.case'));
  const query = document.getElementById('search');
  const selectors = ['category', 'source-kind', 'classification', 'state'];
  const normalize = text => text.normalize('NFKC').toLocaleLowerCase('ja');
  const searchable = cards.map(card => normalize(card.textContent));
  function update() {
    const terms = normalize(query.value).trim().split(/\\s+/).filter(Boolean);
    let shown = 0;
    cards.forEach((card, index) => {
      const selected = selectors.every(key => {
        const value = document.getElementById(key).value;
        return !value || card.getAttribute('data-' + key) === value;
      });
      const visible = selected && terms.every(term => searchable[index].includes(term));
      card.hidden = !visible;
      if (visible) shown++;
    });
    document.getElementById('shown-count').textContent = String(shown);
    document.getElementById('empty').hidden = shown !== 0;
  }
  function reset() {
    form.reset();
    update();
    query.focus();
  }
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  form.addEventListener('submit', event => event.preventDefault());
  document.querySelectorAll('[data-reset]').forEach(button => button.addEventListener('click', reset));
  form.hidden = false;
  document.getElementById('reset').hidden = false;
  update();
})();
'''


def render_html(manifest):
    """Render complete escaped HTML; the browser never fetches its manifest."""
    cases = manifest['cases']
    published_version = manifest['published_version']
    slugs = [case['slug'] for case in cases]
    if len(set(slugs)) != len(slugs):
        raise ValueError('Gallery case IDs must be unique')
    rows = '\n'.join(_case(case, index, published_version) for index, case in enumerate(cases, 1))
    filters = ''.join(
        f'<label>{label}<select id="{element}" name="{element}"><option value="">すべて</option>'
        f'{_options((case[key] for case in cases), labels)}</select></label>'
        for key, element, label, labels in (
            ('category', 'category', '情報の種類', CATEGORIES),
            ('source_kind', 'source-kind', 'データの由来', SOURCES),
            ('classification', 'classification', '電文の区分', CLASSIFICATIONS),
            ('state', 'state', '状態', STATES)))
    categories = len({case['category'] for case in cases})
    license_link = (f'<br><a href="{_local_path(manifest["source_license_path"])}" '
                    'target="_blank" rel="noopener">受信ログのライセンス</a>'
                    if manifest.get('source_license_path') else '')
    return f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>画像ギャラリー | Shirasu</title>
<style>{_CSS}</style></head><body>
<header class="page-header"><div class="header-inner"><div class="heading-line"><h1>画像ギャラリー</h1>
<a href="manifest.json" target="_blank" rel="noopener">一覧データ JSON ↗</a></div>
<p class="intro">同じ入力データで、公開済み {_escape(published_version)} と作業版を比較できます。</p>
<p class="notice">過去の受信・訓練・合成サンプル / 現在の災害情報ではありません</p></div></header>
<main><section class="toolbar" aria-label="表示するサンプルを選択"><div class="toolbar-top">
<p role="status" aria-live="polite" aria-atomic="true"><strong id="shown-count">{len(cases)}</strong> / {len(cases)} 件を表示 <span>· {categories} 種類</span></p>
<button id="reset" type="button" class="reset" data-reset hidden>条件をリセット</button></div>
<form id="filters" class="filters" hidden><label class="search">キーワード検索<input id="search" name="search" type="search" placeholder="例：台風、阿蘇山、解除" autocomplete="off"></label>{filters}</form>
</section><noscript><p class="no-script">すべてのサンプルを表示しています。絞り込みにはJavaScriptが必要です。</p></noscript>
<div id="cases">{rows}</div>
<section id="empty" class="empty" hidden><h2>該当するサンプルがありません</h2><p>検索語や絞り込み条件を変更してください。</p><button type="button" class="reset" data-reset>条件をリセット</button></section>
</main><footer class="page-footer"><p>Shirasu · オフライン画像レビュー<br>南海トラフはテキストのみのため、この一覧には含みません。</p><p>画像を選ぶと原寸で開きます。出典と入力電文は各サンプルから確認できます。{license_link}</p></footer>
<script>{_JS}</script></body></html>
'''
