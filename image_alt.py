"""Short, factual ALT for the received records behind a published image.

This describes bulletin metadata, not a particular renderer or inferred map
extent. It performs no rendering, signing, configuration or network access.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import azarashi
from azarashi.definitions.camf.a4_hazard_category_and_type import a4_hazard_type
from azarashi.definitions.qzss.dcr.marine_warning_code import marine_warning_code
from azarashi.reports import dcr, dcx

from image_protocol import _records


DEFAULT_IMAGE_ALT = 'みちびき受信通報の図'
JST = timezone(timedelta(hours=9))
_DCR_TITLES = {
    dcr.EarthquakeEarlyWarning: '緊急地震速報', dcr.Hypocenter: '震源情報',
    dcr.SeismicIntensity: '震度情報', dcr.Tsunami: '津波情報',
    dcr.NorthwestPacificTsunami: '北西太平洋津波情報', dcr.Volcano: '火山情報',
    dcr.AshFall: '降灰予報', dcr.Weather: '気象警報等',
    dcr.Flood: '指定河川洪水予報', dcr.Marine: '海上警報', dcr.Typhoon: '台風情報',
}
_DCX_TITLES = {dcx.LAlert: 'Lアラート', dcx.JAlert: 'Jアラート',
               dcx.MTInfo: '自治体情報', dcx.OutsideJapan: '海外向け防災情報'}
# These are hazard names, not invented Japanese warning/advisory levels.
# Other defined A4 values retain the decoder table's original English name.
_HAZARDS = {1: '空襲', 4: '生物学的危険', 5: '化学物質', 6: '爆発物',
            8: 'ミサイル', 9: '原子力災害', 10: '原発事故', 11: '放射線',
            13: 'サイレン試験', 24: '大規模火災', 27: '森林火災',
            32: '降灰', 35: '土石流', 36: '地震', 39: '地すべり',
            44: '津波', 46: '噴火', 47: '風・波・高潮', 58: '停電',
            62: '沿岸浸水', 68: '洪水', 72: '雷', 74: '降雨',
            76: '降雪', 79: '竜巻', 80: 'ハリケーン', 82: '台風',
            95: '安全に関する警告', 113: '試験情報'}


@dataclass(frozen=True)
class _Facts:
    title: str
    family: str
    training: bool | None
    state: str | None
    release: str
    when: datetime | None
    notes: tuple[str, ...] = ()


def _known(value, choices):
    return type(value) is int and value in choices


def _release(report):
    """Only explicit release fields count; a low hazard value is not all-clear."""
    if isinstance(report, dcr.Weather):
        value = report.weather_warning_state_raw
        return {1: 'none', 2: 'all'}.get(value, 'unknown') if type(value) is int else 'unknown'
    if isinstance(report, dcr.Tsunami):
        value = report.tsunami_warning_code_raw
        return ('all' if value == 2 else 'none') if _known(value, range(1, 6)) else 'unknown'
    if isinstance(report, (dcr.Flood, dcr.Marine)):
        flood = isinstance(report, dcr.Flood)
        values = report.flood_warning_levels_raw if flood else report.marine_warning_codes_raw
        regions = report.flood_forecast_regions_raw if flood else report.marine_forecast_regions_raw
        known = range(1, 5) if flood else marine_warning_code
        if (not isinstance(values, (list, tuple)) or not isinstance(regions, (list, tuple))
                or not values or len(values) != len(regions)
                or any(not _known(value, known) for value in values)):
            return 'unknown'
        flags = [value == (1 if flood else 0) for value in values]
        return 'all' if all(flags) else 'part' if any(flags) else 'none'
    # Volcano level 1 and NWP's no-tsunami potential are not release fields.
    return 'none'


def _facts(report):
    kind = type(report)
    if kind in _DCR_TITLES:
        title, notes = _DCR_TITLES[kind], []
        if report.version != 1:
            raise ValueError('Unsupported DCR metadata version')
        classification, state = report.report_classification_no, report.information_type_no
        training = classification == 7 if _known(classification, (1, 2, 3, 7)) else None
        state = {0: '発表', 1: '訂正', 2: '取消'}.get(state) if type(state) is int else None
        if kind is dcr.Typhoon:
            number = report.typhoon_number_raw
            if _known(number, range(1, 100)):
                title = f'台風{number}号'
            else:
                notes.append('台風の号数は確認できません。')
        # A cancellation never reuses old warning levels as a live release.
        release = _release(report) if state in ('発表', '訂正') else 'none'
        if kind is dcr.Flood and release in ('all', 'part'):
            notes.append('洪水の警報解除には注意情報への切替を含みます。')
        return _Facts(title, 'dcr', training, state, release, report.report_time, tuple(notes))
    if kind in _DCX_TITLES:
        title, camf = _DCX_TITLES[kind], report.camf
        # A6/A7 is hazard onset, never the issue time. No issue timestamp is
        # exposed by these layouts, so use only the explicitly labelled receipt.
        if (not _known(camf.vn, (0, 1)) or report.dcx_version != camf.vn):
            return _Facts(title, 'dcx', None, None, 'none', report.timestamp,
                          ('未対応のDCXバージョンです。',))
        state = {0: '発表', 1: '発表', 2: '更新', 3: '解除'}.get(camf.a1) if type(camf.a1) is int else None
        training = camf.a1 == 0 if _known(camf.a1, range(4)) else None
        if _known(camf.a10, (0,)) and _known(camf.a4, a4_hazard_type) and camf.a4 != 0:
            title += '（' + _HAZARDS.get(camf.a4, a4_hazard_type[camf.a4]) + '）'
            notes = ()
        else:
            notes = ('災害種別は確認できません。',)
        return _Facts(title, 'dcx', training, state, 'none', report.timestamp, notes)
    raise ValueError('No image ALT metadata for this report type')


def _time(facts):
    families = {item.family for item in facts}
    if len(families) != 1:
        return '発表日時と受信基準日時が混在するため、単一の日時は示せません。'
    label = '発表日時' if families == {'dcr'} else '受信基準日時'
    values = [item.when for item in facts]
    if any(not isinstance(value, datetime) or value.utcoffset() is None for value in values):
        return label + 'を確認できません。'
    if len(set(values)) != 1:
        return label + 'が複数あります。各日時は投稿本文を参照。'
    return label + '：' + values[0].astimezone(JST).strftime('%Y年%m月%d日%H時%M分') + '（JST）。'


def _describe(facts):
    markers, sentences, notes = [], [], []
    training = {item.training for item in facts}
    if training == {True}:
        markers.append('訓練・試験')
    elif True in training:
        markers.append('訓練・試験を含む')
    if None in training:
        notes.append('一部の通報区分は確認できません。')
    states = {item.state for item in facts}
    if len(states) == 1 and None not in states:
        state = next(iter(states))
        if state != '発表':
            markers.append(state)
        if state == '取消':
            sentences.append('この情報は取り消されました。')
        elif state == '解除':
            sentences.append('解除の通知です。')
    elif len(states) > 1:
        markers.append('状態混在')
        known = [state for state in ('発表', '訂正', '取消', '更新', '解除') if state in states]
        if known:
            sentences.append('含まれる状態：' + '・'.join(known) + '。')
    if None in states:
        notes.append('一部の情報形態は確認できません。')
    releases = {item.release for item in facts}
    if releases == {'all'}:
        markers.append('解除')
        sentences.append('受信した警報等の解除情報です。')
    elif releases & {'all', 'part'}:
        markers.append('解除情報を含む')
        sentences.append('受信情報には解除と他の区分が含まれます。')
    if 'unknown' in releases:
        notes.append('一部の解除区分は確認できません。')
    titles = {item.title for item in facts}
    title = next(iter(titles)) if len(titles) == 1 else '複数種類または異なる対象の情報'
    notes.extend(note for item in facts for note in item.notes)
    # All variable labels come from fixed tables. Preserve status/time/unknown
    # caveats first if a long English DCX hazard name makes the paragraph large.
    suffix = ''.join(sentences) + _time(facts) + ''.join(dict.fromkeys(notes)) + '詳細は投稿本文を参照。'
    prefix = ''.join('【' + marker + '】' for marker in markers)
    if len(prefix + title + 'の受信情報。' + suffix) > 300:
        title = 'みちびき通報'
    return prefix + title + 'の受信情報。' + suffix


def image_alt_text(entries):
    """Describe ``(original_nmea, received_ms)`` image entries in one paragraph.

    Invalid entries or unsupported report types raise ValueError for the
    caller's DEFAULT_IMAGE_ALT fallback. Input order does not affect wording.
    Nankai is text-only: reject it *before* decoding, so this extra decode can
    never touch azarashi's process-wide, concurrently used Nankai assembly.
    """
    records = _records(entries)
    for nmea, _ in records:
        raw = int(nmea.split(',')[2].split('*')[0] + '0', 16)
        if (raw >> (256 - 14)) & 63 == 43 and (raw >> (256 - 21)) & 15 == 4:
            raise ValueError('Nankai bulletins use text-only publication')
    try:
        facts = tuple(_facts(azarashi.decode(nmea, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc)))
                      for nmea, ms in records)
    except azarashi.AzarashiException as error:
        raise ValueError('Unable to decode image ALT metadata') from error
    return _describe(facts)
