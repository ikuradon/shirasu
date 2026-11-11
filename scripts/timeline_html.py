"""Self-contained, escaped HTML for synthetic reception and posting timelines."""

from scripts.gallery_html import (
    _CSS as GALLERY_CSS, _alt_details, _escape, _local_path,
    _post_attachment, _post_content,
)


def _elapsed(seconds):
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'T+{hours:02d}:{minutes:02d}:{seconds:02d}'


def _post(case, post, elapsed):
    image = post['images'].get('preview')
    attachment = _post_attachment(case, image, '作業版') if image else None
    content = _post_content(post['text'], attachment)
    content_id = _escape(f'{case["slug"]}-post-{post["number"]}')
    kind = '画像付き' if image else '本文のみ'
    indexes = '・'.join(str(i + 1) for i in post['record_indexes'])
    return (
        f'<section class="timeline-post" data-post-number="{post["number"]}">'
        f'<h4>投稿 {post["number"]} <span>{_elapsed(elapsed)} · {kind}</span></h4>'
        '<div class="post-viewport" tabindex="0" role="group" aria-label="投稿本文と添付画像の表示領域">'
        '<div class="post-frame"><div class="post-fold"><div class="post-clip">'
        f'<div class="post-body" id="{content_id}">{content}</div></div>'
        f'<button class="post-toggle" type="button" aria-controls="{content_id}" '
        'aria-expanded="true" hidden>元に戻す</button></div></div></div>'
        f'<p class="input-note">投稿に含む入力 {indexes}</p>'
        f'{_alt_details(image) if image else ""}</section>'
    )


def _scenario(case):
    events = []
    outcome_labels = {
        'buffered': '収集処理へ追加', 'duplicate': '重複・旧報として抑制',
        'queued': '投稿対象', 'ignored': '対象外',
    }
    for index, step in enumerate(case['steps']):
        elapsed = step['elapsed_seconds']
        receipts = [f'<li><strong>入力 {i + 1}</strong> {_escape(case["receipt_labels"][i])}'
                    f'<span class="outcome">{outcome_labels[outcome]}</span></li>'
                    for i, outcome in zip(step['received_indexes'], step['outcomes'], strict=True)]
        if receipts:
            activity = '<h3>受信</h3><ul class="receipts">' + ''.join(receipts) + '</ul>'
        elif step['posts']:
            activity = '<h3>待機期限</h3><p>収集処理が投稿を確定しました。</p>'
        elif elapsed == case['until_seconds']:
            activity = '<h3>確認終了</h3><p>この時点まで追加の投稿はありません。</p>'
        else:
            activity = '<h3>保存状態の更新</h3><p>保存期限などの処理を行いました。</p>'
        nankai, typhoon = step['pending_nankai'], step['pending_typhoon']
        waiting = []
        if nankai:
            waiting.append(f'南海トラフ {nankai}ページを保持して完成待ち')
        if typhoon:
            waiting.append(f'台風 {typhoon}電文を収集中')
        state = ' / '.join(waiting) if waiting else '収集中の電文なし'
        posts = ''.join(_post(case, post, elapsed) for post in step['posts'])
        if not posts:
            posts = '<p class="no-post">この時点の投稿なし</p>'
        events.append(
            f'<li class="timeline-step" data-has-posts="{str(bool(step["posts"])).lower()}" '
            f'data-elapsed="{elapsed}" id="{_escape(case["slug"])}-step-{index}">'
            f'<div class="step-time">{_elapsed(elapsed)}</div><div class="step-grid">'
            f'<div class="reception">{activity}<p class="collection-state">{state}</p></div>'
            f'<div class="delivery">{posts}</div></div></li>'
        )
    source = ''.join('<li>' + _escape(item['label']) + '<br>'
                     + _escape(item['transformation']) + '</li>' for item in case['sources'])
    times = ' → '.join(_elapsed(step['elapsed_seconds'])
                       for step in case['steps'] for _ in step['posts'])
    return (
        f'<article class="scenario" id="{_escape(case["slug"])}">'
        '<header class="scenario-heading"><p class="eyebrow">合成訓練シナリオ</p>'
        f'<h2>{_escape(case["title"])}</h2><p>{_escape(case["description"])}</p>'
        f'<p class="post-summary">{case["post_count"]}投稿 <span>{times or "投稿なし"}</span></p></header>'
        '<ol class="timeline">' + ''.join(events) + '</ol>'
        '<details class="source-details"><summary>シナリオの出典と入力</summary>'
        f'<div class="details-content"><ul>{source}</ul><p>受信間隔は合成です。電文の受信基準日時は'
        '日付復号のための原値を保持し、経過時間には使用していません。</p>'
        f'<a href="{_local_path(case["records_path"])}">入力・受信間隔のJSON</a> · '
        '<a href="LICENSE.azarashi">出典ライセンス</a></div></details></article>'
    )


CSS = '''
.header-inner,main,.page-footer{max-width:1180px}.heading-line h1{font-size:29px}
.eyebrow{color:var(--muted);font-size:12px;letter-spacing:.08em;margin:0 0 6px}
.timeline-controls{display:flex;gap:16px 24px;flex-wrap:wrap;align-items:center;background:white;border:1px solid var(--line);padding:16px 20px;border-radius:10px;margin-bottom:24px}
.timeline-controls label{display:flex;gap:8px;align-items:center;font-size:13px;max-width:100%}
.timeline-controls select{border:1px solid #cbd4dc;border-radius:5px;background:white;padding:8px;min-width:0;max-width:100%}
.scenario{background:var(--paper);border:1px solid var(--line);border-radius:12px;margin-bottom:26px;overflow:hidden}
.scenario-heading{padding:24px 26px 20px;border-bottom:1px solid var(--line)}.scenario-heading h2{font-size:23px;margin:0 0 8px}.scenario-heading>p:not(.eyebrow){margin:8px 0 0;color:var(--muted)}
.scenario-heading .post-summary{font-weight:650;color:var(--blue)!important}.post-summary span{margin-left:16px;font-size:13px;font-weight:400}
.timeline{list-style:none;padding:8px 26px 20px;margin:0}.timeline-step{position:relative;border-left:2px solid #dce5ec;padding:18px 0 20px 23px;margin-left:6px}.timeline-step::before{content:"";position:absolute;left:-6px;top:25px;width:10px;height:10px;background:#8ba0b3;border:2px solid white;border-radius:50%}.timeline-step[data-has-posts=true]::before{background:#277bb5;box-shadow:0 0 0 3px #e8f1f7}
.step-time{font-variant-numeric:tabular-nums;letter-spacing:.06em;font-size:13px;font-weight:650;color:var(--blue);margin-bottom:10px}
.step-grid{display:grid;grid-template-columns:minmax(200px,1fr) minmax(0,480px);gap:30px}.reception h3{font-size:14px;margin:0 0 7px}.reception p{font-size:13px;margin:4px 0}.receipts{list-style:none;padding:0;margin:0;font-size:14px}.receipts strong{font-size:12px;color:var(--muted);padding-right:7px}.outcome{display:block;font-size:12px;color:#47715c;margin-top:3px}.reception .collection-state{font-size:12px;color:var(--muted);margin-top:14px}
.delivery{min-width:0}.no-post{color:#788795;font-size:13px;margin:0;padding-top:1px}.timeline-post h4{font-size:14px;margin:0 0 8px;display:flex;justify-content:space-between;gap:10px;max-width:var(--image-width,100%);margin-inline:auto}.timeline-post h4 span{font-size:12px;font-weight:400;color:var(--muted)}.timeline-post+.timeline-post{margin-top:24px}.input-note{font-size:11px;color:var(--muted);margin:7px auto;max-width:var(--image-width,100%)}.timeline-post .image-alt{max-width:var(--image-width,100%);margin-inline:auto}
.timeline-post .image-alt summary{font-size:12px}.page-footer{display:block}.page-footer p+p{margin-top:6px}
@media(max-width:740px){.step-grid{grid-template-columns:minmax(0,1fr);gap:14px}.scenario-heading{padding:20px 18px}.scenario-heading h2{font-size:20px}.scenario-heading>p{font-size:13px}.timeline{padding:6px 14px 18px}.timeline-step{padding-left:16px}.post-summary span{display:block;margin:3px 0 0}.timeline-post h4{flex-wrap:wrap}.timeline-controls{padding:14px}.timeline-controls label:first-child{width:100%}.timeline-controls select#scenario{flex:1}.no-post{padding:6px 0}.heading-line h1{font-size:24px}}
'''


JS = '''
(() => {
  const scenario = document.getElementById('scenario');
  const postsOnly = document.getElementById('posts-only');
  const width = document.getElementById('image-width');
  const folds = Array.from(document.querySelectorAll('.post-fold'));
  function layout() {
    folds.forEach(fold => {
      if (!fold.getClientRects().length) return;
      const clip = fold.querySelector('.post-clip');
      const button = fold.querySelector('.post-toggle');
      const long = clip.scrollHeight > 480;
      const expanded = fold.dataset.expanded === 'true';
      fold.dataset.folded = String(long && !expanded);
      button.hidden = !long;
      button.textContent = expanded ? '元に戻す' : 'もっと見る';
      button.setAttribute('aria-expanded', String(expanded || !long));
    });
  }
  function update() {
    document.querySelectorAll('.scenario').forEach(card => {
      card.hidden = Boolean(scenario.value && scenario.value !== card.id);
    });
    document.querySelectorAll('.timeline-step').forEach(step => {
      step.hidden = postsOnly.checked && step.dataset.hasPosts !== 'true';
    });
    if (width.value === 'normal') {
      delete document.body.dataset.imageWidth;
      document.body.style.removeProperty('--image-width');
    } else {
      document.body.dataset.imageWidth = width.value;
      document.body.style.setProperty('--image-width', width.value + 'px');
    }
    requestAnimationFrame(layout);
  }
  folds.forEach(fold => {
    fold.querySelector('.post-toggle').addEventListener('click', () => {
      fold.dataset.expanded = String(fold.dataset.expanded !== 'true');
      layout();
    });
  });
  document.querySelectorAll('img').forEach(img => img.addEventListener('load', layout));
  document.getElementById('timeline-controls').addEventListener('change', update);
  window.addEventListener('resize', layout);
  if (document.fonts) document.fonts.ready.then(layout);
  document.getElementById('timeline-controls').hidden = false;
  update();
})();
'''


def render_html(manifest):
    cases = manifest['scenarios']
    options = ''.join(f'<option value="{_escape(case["slug"])}">{_escape(case["title"])}</option>'
                      for case in cases)
    errors = (f'<p class="notice" role="alert">画像の描画に{manifest["error_count"]}件失敗しました。'
              '各投稿のエラーを確認してください。</p>' if manifest['error_count'] else '')
    return f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>受信経過の投稿プレビュー | Shirasu</title><style>{GALLERY_CSS}{CSS}</style></head>
<body data-view-mode="posts"><header class="page-header"><div class="header-inner">
<p class="eyebrow">SHIRASU / OFFLINE PREVIEW</p><div class="heading-line"><h1>受信経過の投稿プレビュー</h1></div>
<p class="intro">受信から投稿までを仮想時計で確認。{len(cases)}シナリオ・{sum(case['post_count'] for case in cases)}投稿。</p>
<p class="notice">受信間隔を設定した合成訓練例です。T+ は最初の受信からの経過時間で、実際の投稿時刻ではありません。</p>{errors}
</div></header><main>
<div class="timeline-controls" id="timeline-controls" hidden>
<label for="scenario">シナリオ<select id="scenario"><option value="">すべて</option>{options}</select></label>
<label for="image-width">投稿幅<select id="image-width"><option value="320">320px</option><option value="360" selected>360px</option><option value="430">430px</option><option value="normal">通常</option></select></label>
<label><input type="checkbox" id="posts-only">投稿のある時点のみ</label></div>
<noscript><p>全シナリオ・全投稿を表示しています。</p></noscript>
{''.join(_scenario(case) for case in cases)}</main>
<footer class="page-footer"><p>履歴なし・収集待機180秒・配送は即時成功の条件です。同時刻では期限処理を先に行い、新しく受け付けた電文で投稿処理を起こします。重複・対象外の受信では起こさず、待機中は1秒ごとに確認する仮定です。</p>
<p>画像は未公開の作業版です。URLの位置に添付画像を表示し、NMEAを含む本文は実際の投稿と同じ内容です。</p>
<p><a href="manifest.json">全結果のJSON</a> · <a href="LICENSE.azarashi">出典ライセンス</a></p></footer>
<script>{JS}</script></body></html>'''
