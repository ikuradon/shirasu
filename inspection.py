"""One-shot, human-readable views of saved state and delivery history."""

from datetime import datetime, timedelta, timezone
import json
import math
import unicodedata

import azarashi


JST = timezone(timedelta(hours=9), 'JST')
STATES = {'pending': '送信待ち', 'sent': '送信済み', 'expired': '期限切れ'}


def _clean(value):
    # Relay errors may contain newlines, terminal controls or bidi controls.
    return ' '.join(''.join(char if char.isprintable() else ' '
                            for char in str(value)).split())


def _width(text):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
               for char in text)


def _clip(text, width):
    text = _clean(text)
    if _width(text) <= width:
        return text
    result = ''
    for char in text:
        if _width(result + char) > width - 1:
            break
        result += char
    return result + '…'


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _duration(seconds):
    for size, unit in ((86400, '日'), (3600, '時間'), (60, '分')):
        if seconds >= size:
            return f'{int(seconds // size)}{unit}'
    return f'{int(seconds)}秒'


def _relative(timestamp, now, missing):
    if timestamp is None:
        return missing
    timestamp = _number(timestamp)
    if timestamp is None:
        return '不明'
    age = now - timestamp
    if age < 0:
        return f'{_duration(-age)}後（時計を確認）'
    return f'{_duration(age)}前'


def _connection(value):
    if value is None:
        return '未記録'
    return ('接続中' if value else '切断') + '（最終記録）'


def format_status(snapshot, now):
    """Render a saved snapshot; never imply stale connections are current."""
    state = snapshot['status']
    counts = snapshot['counts']
    heartbeat = _number(state.get('heartbeat'))
    fresh = heartbeat is not None and 0 <= now - heartbeat < 90
    running = state.get('running')
    process = '未記録' if running is None else ('稼働' if running else '停止') + '（最終記録）'
    pending_age = _number(snapshot.get('oldest_pending_age_seconds'))
    rows = [
        ('状態', '正常' if snapshot.get('healthy') and fresh else '要確認'),
        ('本体', process),
        ('GPSD', _connection(state.get('gpsd_connected'))),
        ('Nostr', _connection(state.get('relay_connected'))),
        ('最終状態更新', _relative(state.get('heartbeat'), now, '未記録')),
        ('最終受信', _relative(state.get('last_received'), now, '未受信')),
        ('最終送信', _relative(state.get('last_published'), now, '未送信')),
        ('送信待ち', f"{counts.get('pending', 0)}件"),
        ('最古の送信待ち', '-' if pending_age is None else _duration(max(0, pending_age))),
        ('送信済み', f"{counts.get('sent', 0)}件"),
        ('期限切れ', f"{counts.get('expired', 0)}件"),
        ('保存失敗', f"{state.get('dropped_full', 0)}件"),
        ('受信エラー', state.get('last_receiver_error') or 'なし'),
        ('配送エラー', state.get('last_delivery_error') or 'なし'),
    ]
    if 'images_enabled' in state:
        rows.extend([
            ('画像URL', '有効' if state['images_enabled'] else '無効'),
            ('台風の集約待ち', f"{state.get('typhoon_buffered_reports', 0)}通"),
            ('画像URL生成エラー', state.get('last_image_error') or 'なし'),
        ])
    if 'nankai_buffered_reports' in state:
        rows.append(('南海トラフの集約待ち', f"{state['nankai_buffered_reports']}通"))
    width = max(_width(label) for label, _ in rows)
    lines = [label + ' ' * (width - _width(label) + 2) + _clip(value, 100)
             for label, value in rows]
    if not fresh:
        lines.append('状態更新が未記録・90秒以上前・未来のいずれかです。接続表示は現在の状態を保証しません。')
    return '\n'.join(lines)


def _report_description(record):
    try:
        event = json.loads(record['event_json'])
        content = event['content']
        sentence = content.rsplit('\nNMEA: ', 1)[1].strip()
        received_at = datetime.fromtimestamp(record['created_at'], timezone.utc)
        report = azarashi.decode(sentence, timestamp=received_at)
        report_type = type(report).__name__
        label = getattr(report, 'disaster_category', None)
        label = label.strip() if isinstance(label, str) else ''
        return report_type, label or report_type
    except Exception:
        # A malformed or no-longer-supported historic report must remain visible.
        return '不明', '不明'


def prepare_history(history):
    """Add report types and labels without exposing full signed events or mutating input."""
    records = []
    for original in history['events']:
        record = {name: value for name, value in original.items() if name != 'event_json'}
        record['report_type'], record['report_label'] = _report_description(original)
        records.append(record)
    return dict(history, events=records)


def prepare_event(detail):
    """Expose the saved body verbatim; decoding is only used for its type label."""
    original = detail['event']
    record = {name: value for name, value in original.items() if name != 'event_json'}
    record['report_type'], record['report_label'] = _report_description(original)
    record['content'] = None
    record['content_error'] = None
    try:
        event = json.loads(original['event_json'])
    except (KeyError, TypeError, ValueError, RecursionError):
        record['content_error'] = '保存された投稿のJSONを読み取れません。'
    else:
        if not isinstance(event, dict) or not isinstance(event.get('content'), str):
            record['content_error'] = '保存された投稿に文字列の本文がありません。'
        else:
            record['content'] = event['content']
    return dict(detail, event=record)


def _datetime(timestamp):
    try:
        return datetime.fromtimestamp(timestamp, JST).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OverflowError, OSError):
        return '不明'


def format_backup(summary, *, created=False):
    """Describe archive integrity separately from the daemon's live health."""
    counts = summary['counts']
    last_updated = summary['status_updated_at']
    rows = [
        ('ファイル', summary['path']),
        ('作成日時(JST)', _datetime(summary['backup_created_at'])),
        ('状態記録の最終更新(JST)', '未記録' if last_updated is None else _datetime(last_updated)),
        ('投稿アカウント', summary['identity']),
        ('保存アカウント数', str(len(summary['accounts']))),
        ('送信待ち（全体）', f"{counts['pending']}件"),
        ('送信済み（全体）', f"{counts['sent']}件"),
        ('期限切れ（全体）', f"{counts['expired']}件"),
        ('署名検証', f"{summary['events_verified']}件 正常"),
        ('DB整合性', '正常'),
    ]
    width = max(_width(label) for label, _ in rows)
    lines = ['バックアップを作成しました。' if created else 'バックアップを検証しました。']
    lines.extend(label + ' ' * (width - _width(label) + 2) + _clean(value)
                 for label, value in rows)
    lines.append('復元時は、バックアップ取得後の送信履歴を照合してから公開投稿を再開してください。')
    return '\n'.join(lines)


def _escape_body_controls(content):
    # Preserve ordinary whitespace and line breaks, but never execute terminal
    # controls or let bidi/zero-width controls alter the surrounding display.
    return ''.join(
        char if char == '\n' or char.isprintable() or unicodedata.category(char) == 'Zs'
        else (f'\\u{ord(char):04x}' if ord(char) <= 0xffff else f'\\U{ord(char):08x}')
        for char in content
    )


def format_event(detail):
    """Render one saved publication and its delivery metadata without truncation."""
    record = detail['event']
    rows = [
        ('イベントID', record['event_id']),
        ('通報種別', record.get('report_label') or record['report_type']),
        ('配送状態', STATES.get(record['state'], record['state'])),
        ('受信日時(JST)', _datetime(record['created_at'])),
        ('送信日時(JST)', '未送信' if record['sent_at'] is None else _datetime(record['sent_at'])),
        ('失敗回数', str(record['attempts'])),
        ('最終失敗理由', record.get('last_error') or 'なし'),
    ]
    width = max(_width(label) for label, _ in rows)
    lines = [label + ' ' * (width - _width(label) + 2) + _clean(value)
             for label, value in rows]
    lines.extend(('', '投稿本文（保存当時）'))
    content = record['content']
    if content is None:
        lines.append(_clean(record['content_error']))
    elif content == '':
        lines.append('（本文は空です）')
    else:
        body = _escape_body_controls(content)
        if body != content:
            lines.append('※ 制御文字をエスケープ表示しています。JSON形式では元の本文を取得できます。')
        lines.append(body)
    return '\n'.join(lines)


def format_history(history):
    """Render metadata only, in the order supplied by the read-only query."""
    records = history['events']
    summary = f"表示 {len(records)}件 / 対象 {history['total']}件（新しい順）"
    if not records:
        return '配送履歴はありません。\n' + summary
    headings = ['受信日時(JST)', '通報種別', '状態', '失敗回数', 'イベントID', '最終失敗理由']
    rows = [[_datetime(record['created_at']), record.get('report_label') or record['report_type'],
             STATES.get(record['state'], record['state']), str(record['attempts']),
             str(record['event_id'])[:12], record.get('last_error') or '-']
            for record in records]
    limits = (19, 28, 8, 8, 12, 48)
    table = [[_clip(value, limit) for value, limit in zip(row, limits)]
             for row in [headings, *rows]]
    widths = [max(_width(row[column]) for row in table) for column in range(len(headings))]

    def line(row):
        return '  '.join(value + ' ' * (width - _width(value))
                         for value, width in zip(row, widths)).rstrip()

    lines = [summary, line(table[0]), '  '.join('-' * width for width in widths)]
    lines.extend(line(row) for row in table[1:])
    lines.append('失敗回数は配送失敗の記録数です。イベントID・長い項目は省略表示します。')
    return '\n'.join(lines)
