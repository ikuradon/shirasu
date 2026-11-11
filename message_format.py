"""Posting text shared by live delivery and offline previews."""

from datetime import timedelta, timezone

from azarashi.reports import dcr

from dcx_instructions import is_evacuation_destination


JST = timezone(timedelta(hours=9), 'JST')


def _header(report, title):
    training = report.report_classification_no == 7
    cancelled = report.information_type_no == 2
    markers = ('【訓練・試験】' if training else '') + ('【取消】' if cancelled else '')
    if not markers:
        return f'【{title}】{report.information_type}（{report.report_classification}）'
    details = []
    if not cancelled:
        details.append(report.information_type)
    if not training:
        details.append(report.report_classification)
    suffix = f'（{"・".join(details)}）' if details else ''
    return f'{markers}{title}{suffix}'


def _marine_text(report):
    # Never let a malformed pair of arrays silently discard a warning or region.
    lengths = {len(values) for values in (
        report.marine_warning_codes, report.marine_warning_codes_raw,
        report.marine_forecast_regions, report.marine_forecast_regions_raw,
    )}
    if len(lengths) != 1:
        raise ValueError('Marine warning codes and regions must have equal lengths')
    if report.report_time is None or report.report_time.utcoffset() is None:
        raise ValueError('Marine report time must be timezone-aware')
    at = report.report_time.astimezone(JST)
    lines = [_header(report, '海上警報'), f'発表時刻：{at.month}月{at.day}日 {at:%H:%M} JST']
    groups = {}
    for code, label, region in zip(report.marine_warning_codes_raw,
                                   report.marine_warning_codes,
                                   report.marine_forecast_regions, strict=True):
        # Retain both code and label: unknown or distinct codes must not merge
        # merely because their display labels happen to be identical.
        groups.setdefault((code, label), []).append(region)
    for (_, label), regions in groups.items():
        lines.extend(('', f'■ {label}'))
        lines.extend(f'・{region}' for region in regions)
    return '\n'.join(lines)


def _nankai_text(report):
    if report.report_time is None or report.report_time.utcoffset() is None:
        raise ValueError('Nankai report time must be timezone-aware')
    at = report.report_time.astimezone(JST)
    # Read the decoder's assembled text once, while handling this page. It also
    # supplies the received-page count when the announcement is incomplete.
    body = report.extract_text_information()
    return '\n'.join((
        _header(report, '南海トラフ地震関連情報'),
        f'情報種別：{report.information_serial_code}',
        f'発表時刻：{at.month}月{at.day}日 {at:%H:%M} JST',
        '', body,
    ))


def _dcx_evacuation_text(report):
    """Explain the implicit main-area instruction without rewriting raw A11."""
    state = report.camf.a1
    marker = {0: '【訓練・試験】', 1: '【発表】', 2: '【更新】', 3: '【解除】'}[state]
    lines = [marker + 'DCX／避難先の指定']
    if state == 0:
        lines.append('以下は訓練電文に含まれる指示の説明です。')
    if state == 3:
        lines.append('この解除通報には、主範囲から第2楕円の避難先へ移動する'
                     '既定指示（IC-A-03）の設定が含まれています。'
                     '解除の通知であり、新たな避難行動を促すものとして扱いません。')
    else:
        lines.append('主範囲内の人への既定指示（IC-A-03）：危険区域を直ちに離れ、'
                     '受信した第2楕円で指定された避難先へ移動し、'
                     'ラジオなどで案内・情報を確認する、という内容です。')
    lines.extend((
        '第2楕円は通報が指定する避難先です。その範囲内への別の指示がないことと、'
        '主範囲内の人への既定指示がないことは異なります。',
        '主範囲と第2楕円が重なる区域では、主範囲の指示が優先します。',
        '', '受信値の復号テキスト（既定指示の補足は上記）：', str(report),
    ))
    return '\n'.join(lines)


def report_message(report):
    if isinstance(report, dcr.Marine):
        text = _marine_text(report)
    elif isinstance(report, dcr.NankaiTroughEarthquake):
        text = _nankai_text(report)
    elif is_evacuation_destination(report):
        text = _dcx_evacuation_text(report)
    else:
        text = str(report)
    return f'{text}\n---\nNMEA: {report.nmea}'
