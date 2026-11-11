"""Offline timing/codec report. All measurements come from the saved manifest."""

from collections import defaultdict
import json
import math
from statistics import median

from scripts.gallery_html import CATEGORIES, _escape, _local_path


LABELS = {
    'png-original': '元のPNG', 'png-optimized': 'PNG 最適化',
    'webp-lossless': 'WebP 可逆 / method 4', 'webp-lossless-m0': 'WebP 可逆 / method 0',
    'webp-q90': 'WebP Q90',
    'jpeg-q90': 'JPEG Q90 / 4:4:4', 'avif-q90': 'AVIF Q90',
}


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _ms(value):
    return f'{value:,.1f} ms' if _number(value) else '—'


def _size(value):
    return f'{value / 1024:,.1f} KiB' if _number(value) else '—'


def _ratio(size, original):
    return f'{100 * size / original:.1f}%' if _number(size) and _number(original) and original else '—'


def _label(item):
    return LABELS.get(item['id'], item.get('label', item['id']))


def _original(row):
    return next((item for item in row.get('formats', []) if item['id'] == 'png-original'
                 and not item.get('error')), None)


def _summary_table(results):
    def slowest_first(row):
        value = row.get('warm', {}).get('render_ms', {}).get('median')
        measured = not row.get('error') and _number(value)
        return (not measured, -value if measured else 0)

    rows = []
    for row in sorted(results, key=slowest_first):
        original = _original(row)
        candidates = [item for item in row.get('formats', [])
                      if item['id'] in ('webp-lossless', 'webp-lossless-m0')
                      and not item.get('error') and _number(item.get('bytes'))]
        lossless = min(candidates, key=lambda item: item['bytes']) if candidates else None
        cold = row.get('cold', {})
        warm = row.get('warm', {}).get('render_ms', {})
        if row.get('error'):
            measurements = f'<td colspan="6" class="error">{_escape(row["error"])}</td>'
        else:
            measurements = (
                f'<td>{_ms(cold.get("import_ms"))}</td><td>{_ms(cold.get("render_ms"))}</td>'
                f'<td>{_ms(warm.get("median"))}</td>'
                f'<td>{_size(original.get("bytes")) if original else "—"}</td>'
                f'<td title="{_escape(_label(lossless)) if lossless else ""}">'
                f'{_ratio(lossless.get("bytes"), original.get("bytes")) if original and lossless else "—"}</td>'
                f'<td>{"一致" if row.get("stable_png") is True else "不一致" if row.get("stable_png") is False else "—"}</td>'
            )
        rows.append(
            f'<tr><th scope="row"><a href="#image-{_escape(row["id"])}" data-show-image="{_escape(row["id"])}">'
            f'{_escape(row["title"])}</a><small>{_escape(row["id"])}</small></th>{measurements}</tr>'
        )
    return ('<div class="table-scroll" tabindex="0"><table class="timing-table"><thead><tr>'
            '<th scope="col">画像</th><th scope="col">初回 import</th><th scope="col">初回描画</th>'
            '<th scope="col">再描画 中央値</th><th scope="col">元PNG容量</th>'
            '<th scope="col">WebP可逆 最小 / PNG</th><th scope="col">反復PNG</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')


def _format_summary(results):
    groups = defaultdict(list)
    for row in results:
        original = _original(row)
        if not original or not _number(original.get('bytes')):
            continue
        for item in row.get('formats', []):
            if not item.get('error') and _number(item.get('bytes')):
                groups[item['id']].append((item, original['bytes']))
    rows = []
    for name, pairs in groups.items():
        size, baseline = sum(item['bytes'] for item, _ in pairs), sum(base for _, base in pairs)
        encodes = [item.get('encode_ms', {}).get('median') for item, _ in pairs if item.get('encode_ms')]
        encodes = [value for value in encodes if _number(value)]
        decodes = [item.get('decode_ms', {}).get('median') for item, _ in pairs if item.get('decode_ms')]
        decodes = [value for value in decodes if _number(value)]
        exact = sum(item.get('pixel_exact') is True for item, _ in pairs)
        rows.append(
            f'<tr><th scope="row">{_escape(_label(pairs[0][0]))}</th><td>{len(pairs)}</td>'
            f'<td>{size / 1024 / 1024:,.2f} MiB</td><td>{_ratio(size, baseline)}</td>'
            f'<td>{_ms(median(encodes)) if encodes else "描画に含む"}</td>'
            f'<td>{_ms(median(decodes)) if decodes else "—"}</td><td>{exact} / {len(pairs)}</td></tr>'
        )
    return ('<div class="table-scroll" tabindex="0"><table><thead><tr><th scope="col">形式</th>'
            '<th scope="col">成功画像数</th><th scope="col">容量合計</th><th scope="col">元PNG比</th>'
            '<th scope="col">圧縮時間 中央値</th><th scope="col">復号時間 中央値</th>'
            '<th scope="col">画素一致</th></tr></thead><tbody>' + ''.join(rows)
            + '</tbody></table></div>')


def _format_card(row, item, original):
    name = _label(item)
    if item.get('error'):
        content = f'<p class="error">{_escape(item["error"])}</p>'
    else:
        path = _local_path(item['path'])
        width, height = item.get('width'), item.get('height')
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ValueError('Benchmark image dimensions must be positive integers')
        content = (
            '<div class="image-scroll" tabindex="0" role="group" aria-label="画像の比較領域">'
            f'<a href="{path}" target="_blank" rel="noopener" aria-label="{_escape(name)}を原寸で開く">'
            f'<img class="format-image" src="{path}" width="{width}" height="{height}" style="--natural-width:{width}px" '
            f'alt="{_escape(row["title"])} / {_escape(name)}" loading="lazy" decoding="async"></a></div>'
            f'<p class="format-size"><strong>{_size(item.get("bytes"))}</strong>'
            f'<span>元PNG比 {_ratio(item.get("bytes"), original.get("bytes")) if original else "—"}</span></p>'
        )
        exact = item.get('pixel_exact')
        quality = 'RGBA画素が元PNGと完全一致' if exact is True else 'RGBA画素に差分あり' if exact is False else '画素一致は未記録'
        psnr = item.get('psnr_rgb_db')
        if _number(psnr):
            quality += f' · RGB PSNR {psnr:.2f} dB'
        encode = item.get('encode_ms')
        content += (
            f'<p class="quality">{quality}</p><dl class="format-metrics">'
            f'<div><dt>圧縮</dt><dd>{_ms(encode.get("median")) if encode else "PNG生成は描画時間に含む"}</dd></div>'
            f'<div><dt>復号</dt><dd>{_ms((item.get("decode_ms") or {}).get("median"))}</dd></div></dl>'
        )
        if item.get('alpha_flattened'):
            content += '<p class="notice">透明部分を白背景へ合成しています。</p>'
        if item.get('warnings'):
            content += '<ul class="warnings">' + ''.join(f'<li>{_escape(warning)}</li>' for warning in item['warnings']) + '</ul>'
        source_decode, prepare = item.get('source_decode_ms'), item.get('prepare_ms')
        if _number(source_decode) or _number(prepare):
            content += (f'<p class="quality">変換前の処理（各1回）：元PNG復号 {_ms(source_decode)}'
                        f' · 色モード準備 {_ms(prepare)}</p>')
        content += ('<details class="parameters"><summary>エンコード設定</summary><pre>'
                    + _escape(json.dumps(item.get('parameters', {}), ensure_ascii=False, indent=2, allow_nan=False))
                    + '</pre></details>')
    return (f'<section class="format-card" data-format="{_escape(item["id"])}">'
            f'<h3>{_escape(name)}</h3>{content}</section>')


def _image_case(row):
    original = _original(row)
    warm = row.get('warm', {}).get('render_ms', {})
    usages = ', '.join(f'{usage["case_slug"]} / '
                       + (f'投稿{usage.get("post_number", "")}' if usage['role'] == 'post' else '画像のみ')
                       for usage in row.get('usages', []))
    conditions = f'入力{row.get("record_count", 0)}電文'
    if usages:
        conditions += ' · ' + usages
    if row.get('error'):
        content = f'<p class="error">{_escape(row["error"])}</p>'
    else:
        content = '<div class="format-grid">' + ''.join(_format_card(row, item, original)
                                                        for item in row.get('formats', [])) + '</div>'
    records = (f'<a href="{_local_path(row["records_path"])}">原入力JSON</a>' if row.get('records_path') else '')
    rss = row.get('peak_rss_mib')
    rss_text = f'{rss:,.1f} MiB' if _number(rss) else '—'
    save_ms = row.get('warm', {}).get('png_save_ms', {}).get('median')
    save_text = f' · うちPNG圧縮 中央値 {_ms(save_ms)}' if _number(save_ms) else ''
    return (
        f'<article class="image-case" id="image-{_escape(row["id"])}" data-image-id="{_escape(row["id"])}">'
        '<header><p class="eyebrow">' + _escape(CATEGORIES.get(row.get('category'), row.get('category', '')))
        + f'</p><h2>{_escape(row["title"])}</h2><p class="case-context">{_escape(conditions)}</p>'
        f'<p class="case-metrics">再描画 {_ms(warm.get("median"))} '
        f'（{_ms(warm.get("min"))}〜{_ms(warm.get("max"))}）{save_text} · 描画時ピークRSS {rss_text}</p></header>'
        f'{content}<footer>{records}<span>{_escape(row["id"])}</span></footer></article>'
    )


CSS = '''
:root{color-scheme:light;--bg:#f3f5f7;--paper:#fff;--ink:#17232f;--muted:#586777;--line:#dce2e8;--blue:#1d608e;--comparison-width:360px}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 system-ui,-apple-system,"Noto Sans CJK JP","Yu Gothic",sans-serif}a{color:var(--blue);text-underline-offset:3px}button,select{font:inherit}select,summary{cursor:pointer}a:focus-visible,select:focus-visible,summary:focus-visible,[tabindex]:focus-visible{outline:3px solid #3c87bd;outline-offset:3px}[hidden]{display:none!important}
.page-header{background:var(--paper);border-bottom:1px solid var(--line)}.header-inner,main,.page-footer{max-width:1440px;margin:auto;padding-left:32px;padding-right:32px}.header-inner{padding-top:30px;padding-bottom:25px}.eyebrow{font-size:12px;letter-spacing:.08em;color:var(--muted);margin:0 0 5px}h1{font-size:30px;line-height:1.4;margin:0 0 10px}.intro{margin:0;color:var(--muted)}.notice{font-size:12px;color:#795c23;background:#fff8e8;border:1px solid #eadcb9;padding:9px 12px;border-radius:5px}.header-inner>.notice{margin-bottom:0}main{padding-top:25px;padding-bottom:30px}.report-section{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:22px;margin-bottom:24px}.report-section>h2{font-size:21px;margin:0 0 10px}.section-note{font-size:13px;color:var(--muted);margin:0 0 16px}.table-scroll{max-width:100%;overflow-x:auto}table{border-collapse:collapse;min-width:760px;width:100%;font-variant-numeric:tabular-nums;font-size:13px}th,td{padding:11px 14px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}thead th{background:#f7f9fb;font-size:12px;color:var(--muted)}th:first-child{text-align:left}tbody th{font-weight:500}th small{display:block;color:var(--muted);font-weight:400;font-size:10px}.timing-table tbody tr:hover{background:#f7faff}.error{color:#873b49;background:#fff7f8;padding:15px;border-radius:5px;white-space:pre-wrap;overflow-wrap:anywhere;text-align:left}.environment{margin-top:18px}.environment>summary{font-size:13px;color:var(--muted)}pre{background:#f5f7f9;padding:14px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;border-radius:5px}.controls{display:flex;gap:15px 24px;flex-wrap:wrap;align-items:center;border-top:1px solid var(--line);padding-top:18px;margin-top:18px}.controls label{display:flex;align-items:center;gap:8px;font-size:13px;min-width:0;max-width:100%}.controls label:first-child{flex:1 1 360px}.controls select{background:white;border:1px solid #cbd4dc;border-radius:5px;padding:8px 10px;min-width:0;max-width:100%}#image-select{flex:1}.image-case{background:var(--paper);border:1px solid var(--line);border-radius:10px;margin-bottom:24px;overflow:hidden;scroll-margin-top:20px}.image-case>header{padding:22px 24px;border-bottom:1px solid var(--line)}.image-case h2{font-size:23px;margin:0 0 7px}.case-context,.case-metrics{font-size:12px;color:var(--muted);overflow-wrap:anywhere;margin:5px 0}.format-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;padding:22px 24px}.format-card{min-width:0}.format-card h3{font-size:15px;margin:0 0 10px}.image-scroll{overflow:auto;overscroll-behavior:contain;max-height:620px;background:#e8edf1;border:1px solid var(--line);border-radius:5px}.image-scroll a{display:block;width:max-content;margin:auto;line-height:0}.format-image{display:block;width:var(--comparison-width);height:auto;max-width:none}body[data-image-width="natural"] .format-image{width:var(--natural-width)}.format-size{display:flex;gap:12px;justify-content:space-between;font-variant-numeric:tabular-nums;margin:8px 0 0}.format-size strong{font-size:16px}.format-size span{font-size:12px;color:var(--muted)}.quality{font-size:12px;margin:6px 0;color:var(--muted)}.format-metrics{display:flex;gap:18px;font-size:12px;margin:8px 0}.format-metrics>div{display:flex;gap:8px}.format-metrics dt{color:var(--muted)}.format-metrics dd{margin:0}.warnings{font-size:12px;color:#795c23;padding-left:18px}.parameters{font-size:12px;color:var(--muted);margin-top:10px}.parameters pre{margin-bottom:0}.image-case>footer{padding:12px 24px;border-top:1px solid var(--line);font-size:12px;display:flex;justify-content:space-between;color:var(--muted);gap:10px}.image-case>.error{margin:22px 24px}.page-footer{font-size:12px;color:var(--muted);padding-bottom:25px}.page-footer p{margin:6px 0}
.header-links{margin:14px 0 0;font-size:13px}.header-links a{display:inline-block;padding:3px 0}
@media(max-width:760px){.header-inner,main,.page-footer{padding-left:16px;padding-right:16px}.header-inner{padding-top:23px}h1{font-size:25px}.intro{font-size:13px}.report-section{padding:17px 15px}.report-section>h2{font-size:19px}.format-grid{grid-template-columns:minmax(0,1fr);padding:18px 16px;gap:24px}.image-case>header{padding:18px 16px}.image-case h2{font-size:20px}.image-case>footer{padding-left:16px;padding-right:16px;flex-wrap:wrap}.image-scroll{max-height:500px}.controls label{flex-wrap:wrap}.controls label:first-child{flex-basis:100%}.format-size{flex-wrap:wrap}th,td{padding:10px}.format-metrics{flex-wrap:wrap;gap:5px 18px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
'''


JS = '''
(() => {
  const selector = document.getElementById('image-select');
  const format = document.getElementById('format-select');
  const width = document.getElementById('image-width');
  const cards = Array.from(document.querySelectorAll('.image-case'));
  const syncFromOriginal = new Map();
  function update() {
    cards.forEach(card => {
      card.hidden = Boolean(selector.value && card.dataset.imageId !== selector.value);
      card.querySelectorAll('.format-card').forEach(panel => {
        panel.hidden = panel.dataset.format !== 'png-original' && panel.dataset.format !== format.value;
      });
    });
    document.body.dataset.imageWidth = width.value;
    if (width.value !== 'natural') document.body.style.setProperty('--comparison-width', width.value + 'px');
    // Revealing a new codec does not emit a scroll event on the unchanged
    // original. Wait for its new layout, then copy the original's viewport.
    requestAnimationFrame(() => {
      cards.forEach(card => { if (!card.hidden) syncFromOriginal.get(card)?.(); });
    });
  }
  document.getElementById('comparison-controls').addEventListener('change', update);
  document.querySelectorAll('[data-show-image]').forEach(link => {
    link.addEventListener('click', () => { selector.value = link.dataset.showImage; update(); });
  });
  cards.forEach(card => {
    const viewports = Array.from(card.querySelectorAll('.image-scroll'));
    const copiedOffsets = new WeakMap();
    function synchronize(viewport) {
      if (!viewport.getClientRects().length) return;
      viewports.forEach(other => {
        if (other !== viewport && other.getClientRects().length) {
          if (other.scrollLeft !== viewport.scrollLeft || other.scrollTop !== viewport.scrollTop) {
            other.scrollLeft = viewport.scrollLeft;
            other.scrollTop = viewport.scrollTop;
            copiedOffsets.set(other, [other.scrollLeft, other.scrollTop]);
          }
        }
      });
    }
    viewports.forEach(viewport => viewport.addEventListener('scroll', () => {
      const copied = copiedOffsets.get(viewport);
      copiedOffsets.delete(viewport);
      // Ignore only the event produced by our exact copied position. A real
      // scroll to a different position must not be lost during a frame guard.
      if (copied && viewport.scrollLeft === copied[0] && viewport.scrollTop === copied[1]) return;
      synchronize(viewport);
    }));
    const original = card.querySelector('.format-card[data-format="png-original"] .image-scroll');
    syncFromOriginal.set(card, () => { if (original) synchronize(original); });
    card.querySelectorAll('img').forEach(image => image.addEventListener('load', () => {
      if (card.hidden || !image.getClientRects().length) return;
      requestAnimationFrame(() => syncFromOriginal.get(card)?.());
    }));
  });
  const target = cards.find(card => '#' + card.id === window.location.hash);
  if (target) selector.value = target.dataset.imageId;
  document.getElementById('comparison-controls').hidden = false;
  update();
})();
'''


def render_html(manifest):
    results = manifest['results']
    format_options = {}
    for row in results:
        for item in row.get('formats', []):
            if item['id'] != 'png-original':
                format_options.setdefault(item['id'], _label(item))
    selected = 'webp-lossless' if 'webp-lossless' in format_options else next(iter(format_options), '')
    options = ''.join(f'<option value="{_escape(key)}"{" selected" if key == selected else ""}>{_escape(value)}</option>'
                      for key, value in format_options.items())
    image_options = ''.join(f'<option value="{_escape(row["id"])}"{" selected" if index == 0 else ""}>{_escape(row["title"])} / {_escape(row["id"])}</option>'
                            for index, row in enumerate(results))
    settings = manifest.get('settings', {})
    environment = _escape(json.dumps({'environment': manifest.get('environment', {}),
                                      'settings': settings}, ensure_ascii=False, indent=2, allow_nan=False))
    errors = sum(bool(row.get('error')) for row in results)
    recorded_codec_errors = manifest.get('codec_error_count')
    codec_errors = (recorded_codec_errors if type(recorded_codec_errors) is int and recorded_codec_errors >= 0
                    else sum(bool(item.get('error')) for row in results for item in row.get('formats', [])))
    error_notice = (f'<p class="notice" role="alert">描画失敗 {errors}件 / 形式変換失敗 {codec_errors}件。'
                    '詳細のエラーと成功画像数を確認してください。</p>' if errors or codec_errors else '')
    return f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>描画・画像形式の計測 | Shirasu</title><style>{CSS}</style></head>
<body><header class="page-header"><div class="header-inner"><p class="eyebrow">SHIRASU / LOCAL BENCHMARK</p>
<h1>描画時間と画像形式の比較</h1><p class="intro">{len(results)}種類の画像入力を計測。元のPNGと同じ描画結果から各形式へ変換しています。</p>
<p class="header-links"><a href="#comparison">画像比較へ移動 ↓</a></p>
<p class="notice">この実行環境での計測です。Vercelのコールドスタート、HTTP・CDNのキャッシュ応答時間ではありません。</p>{error_notice}</div></header>
<main><section class="report-section"><h2>形式ごとの容量と圧縮時間</h2>
<p class="section-note">容量比は、各形式で成功した画像に対応する元PNGの合計との比較です。時間は画像ごとの中央値をさらに集計した中央値です。画素一致はRGBAの完全一致で判定します。</p>
{_format_summary(results)}
<p class="notice">元PNGの圧縮時間は描画時間に含まれます。別形式の時間はPNG生成後の追加変換の保存処理で、元PNG復号・色モード準備は含みません。直接その形式へ描画した場合の総時間ではありません。PSNRは見やすさの合否判定には使っていません。</p>
<details class="environment"><summary>実行環境・計測条件</summary><pre>{environment}</pre></details></section>
<section class="report-section"><h2>画像ごとの描画時間</h2>
<p class="section-note">再描画の中央値が長い順に表示しています（未計測・失敗は末尾）。各画像を独立したプロセスで起動し、初回のimportと描画を分離。再描画は同じプロセスで{_escape(settings.get('warm_repeats', '複数'))}回実行した中央値です。OSのディスクキャッシュは消去していません。反復PNGは初回と再描画のバイト一致です。</p>
{_summary_table(results)}</section>
<section class="report-section" id="comparison"><h2>同じ画像で見比べる</h2>
<p class="section-note">左が元PNG、右が選択した形式です。SNS幅での文字・線と、等倍での輪郭や色にじみを確認できます。等倍では両画像のスクロール位置を同期します。</p>
<div class="controls" id="comparison-controls" hidden><label>画像<select id="image-select"><option value="">すべて</option>{image_options}</select></label>
<label>比較する形式<select id="format-select">{options}</select></label><label>表示幅<select id="image-width"><option value="320">320px</option><option value="360" selected>360px</option><option value="430">430px</option><option value="natural">等倍</option></select></label></div>
<noscript><p>JavaScriptなしでは、全画像・全形式を表示しています。</p></noscript></section>
{''.join(_image_case(row) for row in results)}</main>
<footer class="page-footer"><p><a href="manifest.json">全計測結果のJSON</a> · <a href="results.csv">画像ごとのCSV</a></p>
<p>画質の数値だけで採用を決めず、警報色・小さな文字・県境などを同じ幅で見比べてください。公開済みURLや画像形式の変更は行っていません。</p></footer>
<script>{JS}</script></body></html>'''
