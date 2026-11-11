"""Curated, reproducible inputs for the offline image gallery.

This module reads only committed examples and the pinned public corpus. It does
not import the receiver, use its configuration, or mutate Nankai assembly state.
The recording timestamps are date anchors, not captured per-frame receive times.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import azarashi
from azarashi.reports import dcr


ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path('tests/fixtures/qzqsm')
RECEIVER = 'dcr_receiver_20260924'
DCX = 'dcx_archive_20260917'


@dataclass(frozen=True)
class GalleryCase:
    slug: str
    title: str
    category: str
    source_kind: str
    classification: str
    state: str
    description: str
    source_label: str
    source_url: str | None
    source_file: str
    records: tuple[tuple[str, int], ...]
    tags: tuple[str, ...]
    source_sha256: str
    cancel_reference: tuple[tuple[str, int], ...] = ()


def _decode(records):
    return tuple(azarashi.decode(nmea, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
                 for nmea, ms in records)


def _validated(case):
    reports = _decode(case.records)
    if not reports or {type(report).__name__ for report in reports} != {case.category}:
        raise ValueError(f'Gallery sample category changed: {case.slug}')
    classification = {'training' if (report.report_classification_no == 7
                       if isinstance(report, dcr.Base) else report.camf.a1 == 0)
                      else 'ordinary' for report in reports}
    if classification != {case.classification}:
        raise ValueError(f'Gallery sample classification changed: {case.slug}')
    if case.source_kind == 'synthetic' and case.classification != 'training':
        raise ValueError('Synthetic gallery samples must be explicitly training messages')
    if len(reports) > 1:
        groups = {(report.report_time, report.report_classification_no, report.information_type_no)
                  for report in reports}
        if len(groups) != 1:
            raise ValueError(f'Gallery sample mixes announcements: {case.slug}')
    for report in reports:
        if isinstance(report, dcr.Base) and report.information_type_no == 2:
            state = 'cancel'
        elif ((isinstance(report, dcr.Flood) and set(report.flood_warning_levels_raw) == {1})
              or (isinstance(report, dcr.Tsunami) and report.tsunami_warning_code_raw == 2)
              or (not isinstance(report, dcr.Base) and report.camf.a1 == 3)):
            state = 'release'
        else:
            state = 'issue'
        if state != case.state:
            raise ValueError(f'Gallery sample state changed: {case.slug}')
    if case.cancel_reference:
        # The catalog has no general cancellation matcher. This explicit
        # relation belongs only to a synthetic fixture's known construction.
        if (case.source_kind, case.classification, case.state) != ('synthetic', 'training', 'cancel'):
            raise ValueError('Gallery cancellation references must be explicit synthetic training pairs')
        references = _decode(case.cancel_reference)
        if (not references or len(references) != len(set(case.cancel_reference))
                or any(not isinstance(report, dcr.Base)
                       or type(report).__name__ != case.category
                       or report.report_classification_no != 7
                       or report.information_type_no not in (0, 1)
                       or report.report_time > reports[0].report_time
                       for report in references)
                or len({(report.report_time, report.information_type_no) for report in references}) != 1):
            raise ValueError('Invalid synthetic cancellation reference bundle')
    return case


def _field_variant(nmea, fields):
    """Change explicit message fields and rebuild CRC-24Q and the NMEA checksum."""
    _, satellite, encoded = nmea.split('*')[0].split(',')
    value = int(encoded + '0', 16)
    for offset, width, replacement in fields:
        if not 0 <= replacement < 1 << width or offset < 0 or offset + width > 226:
            raise ValueError('Invalid gallery fixture field')
        shift = 256 - offset - width
        value = (value & ~(((1 << width) - 1) << shift)) | (replacement << shift)
    data, crc = value.to_bytes(32, 'big'), 0
    for bit in range(226):
        carry = ((crc >> 23) & 1) ^ ((data[bit // 8] >> (7 - bit % 8)) & 1)
        crc = (crc << 1) & 0xffffff
        if carry:
            crc ^= 0x864cfb
    value = (value & ~(((1 << 24) - 1) << 6)) | (crc << 6)
    body = f'QZQSM,{satellite},{value:064X}'[:-1]
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f'${body}*{checksum:02X}'


def _training_variant(nmea, fields):
    """Keep derived DCR messages explicitly marked as training."""
    return _field_variant(nmea, (*fields, (14, 3, 7)))


def _dcx_training_variant(nmea, fields):
    """DCX uses A1 for training; its bits 14–16 belong to satellite designation."""
    return _field_variant(nmea, (*fields, (24, 2, 0)))


def load_cases(root=ROOT):
    """Return a stable catalog; refuse changed recordings before rendering.

    Numbered selections refer to zero-based QZQSM frames, excluding comments.
    The source hashes pin these selections independently of current decoding.
    Nankai is intentionally absent: new Nankai posts use text, not images.
    """
    root = Path(root)
    manifest = json.loads((root / CORPUS / 'manifest.json').read_text(encoding='utf-8'))
    sources = {}
    for name in (RECEIVER, DCX):
        metadata = manifest['recordings'][name]
        source = CORPUS / metadata['file']
        raw = (root / source).read_bytes()
        if hashlib.sha256(raw).hexdigest() != metadata['sha256']:
            raise ValueError(f'Public recording checksum mismatch: {source}')
        ms = round(datetime.fromisoformat(metadata['reference_time']).timestamp() * 1000)
        records = tuple((line, ms) for line in raw.decode('utf-8').splitlines()
                        if line.startswith('$QZQSM,'))
        if len(records) != metadata['frame_count']:
            raise ValueError(f'Public recording frame count changed: {source}')
        sources[name] = (metadata, source, records)

    cases = []

    def recorded(slug, title, category, indices, description, *, source=RECEIVER,
                 classification='training', state='issue', tags=()):
        metadata, file, records = sources[source]
        receiver = source == RECEIVER
        cases.append(_validated(GalleryCase(
            slug, title, category, 'receiver' if receiver else 'archive', classification,
            state, description,
            'ZED-F9P 実受信 / 2026-09-24' if receiver else '公式 L1S 抽出 / 2026-09-17',
            metadata['source_url'], file.as_posix(), tuple(records[index] for index in indices),
            tuple(tags), metadata['sha256'])))

    def synthetic(slug, title, category, filename, description, *, fields=(),
                  state='issue', tags=()):
        file = Path('image_service/examples') / (filename + '.json')
        raw = (root / file).read_bytes()
        rows = json.loads(raw)
        records = tuple((_training_variant(row['nmea'], fields) if fields else row['nmea'],
                         row['received_ms']) for row in rows)
        cases.append(_validated(GalleryCase(
            slug, title, category, 'synthetic', 'training', state, description,
            '同梱の合成訓練例' + ('から派生' if fields else ''), None, file.as_posix(),
            records, tuple(tags), hashlib.sha256(raw).hexdigest())))

    def synthetic_dcx(slug, title, category, index, description, *, fields, tags=(), source=DCX):
        metadata, file, records = sources[source]
        sentence, ms = records[index]
        derived = ((_dcx_training_variant(sentence, fields), ms),)
        origin = '公開受信ログ' if source == RECEIVER else '公式アーカイブ'
        cases.append(_validated(GalleryCase(
            slug, title, category, 'synthetic', 'training', 'issue', description,
            origin + 'から派生した合成訓練例', None, file.as_posix(),
            derived, tuple(tags), metadata['sha256'])))

    recorded('typhoon-track', '台風26号 / 実況と予報', 'Typhoon', range(2, 9),
             '同じ発表の実況・予報7時点をまとめた記録です。',
             classification='ordinary', tags=('複数電文',))
    recorded('hypocenter', '震源情報 / 日向灘', 'Hypocenter', (12,),
             '公式訓練の震源位置・深さ・規模を含む電文です。')
    recorded('weather', '気象 / 竜巻注意情報', 'Weather', (0,),
             '北海道の2区域を含む、過去に受信した通常配信の電文です。',
             classification='ordinary')
    recorded('eew', '緊急地震速報 / 公式訓練', 'EarthquakeEarlyWarning', (11,),
             '多数の予報区域を含む訓練電文です。地図と要旨表示の切替も確認します。',
             tags=('多数区域',))
    recorded('seismic-intensity', '震度情報 / 九州', 'SeismicIntensity', (13,),
             '公式訓練で受信した都道府県ごとの震度情報です。')
    recorded('tsunami-warning', '津波警報 / 公式訓練', 'Tsunami', (14,),
             '長い防災文を含む電文です。画像が要旨の抜粋になる場合もあります。',
             tags=('長文',))
    recorded('tsunami-release', '津波 / 警報解除', 'Tsunami', (43,),
             '警報解除の訓練電文です。誤報の取消とは区別します。', state='release')
    recorded('volcano', '火山 / 阿蘇山', 'Volcano', (46,),
             '公式訓練で受信した阿蘇山の噴火に関する情報です。')
    recorded('ash-detail', '降灰予報 / 阿蘇山・詳細', 'AshFall', range(48, 53),
             '5電文に分かれた3市町村×6時間の18項目をまとめます。', tags=('複数電文',))
    recorded('flood-issue', '洪水 / 指定河川の警戒情報', 'Flood', (56,),
             '指定された予報区間を線で示す訓練電文です。浸水範囲ではありません。')
    recorded('flood-release', '洪水 / 警報解除', 'Flood', (61,),
             '警報解除には氾濫注意情報への切替も含まれ、安全を示すものではありません。',
             state='release')
    synthetic('nwp', '北西太平洋津波 / 予測地点', 'NorthwestPacificTsunami',
              'northwest_pacific_tsunami',
              '公開受信ログに未収録の種別を補う合成訓練例です。予測地点ごとの値を含みます。')
    recorded('marine', '海上警報 / 北海道・東北沖', 'Marine', (9,),
             '過去に受信した海上の予報区域と風警報を含む通常配信です。',
             classification='ordinary', tags=('多数区域',))
    dcx_display = '受信内容に応じて地点・範囲、対象区域、トピック画像の順に表示を自動選択します。'
    recorded('dcx-lalert', 'DCX / Lアラート', 'LAlert', (0,),
             '公式アーカイブ由来の試験電文です。' + dcx_display, source=DCX)
    recorded('dcx-jalert', 'DCX / Jアラート', 'JAlert', (115,),
             '公式アーカイブ由来の試験電文です。' + dcx_display, source=DCX)
    recorded('dcx-mtinfo', 'DCX / 自治体情報', 'MTInfo', (163,),
             '公式アーカイブ由来の試験電文です。' + dcx_display, source=DCX)
    recorded('dcx-outside-japan', 'DCX / 海外通報', 'OutsideJapan', (70,),
             '受信ログに含まれる海外向けの試験電文です。' + dcx_display)
    secondary_origin = ('公開受信ログのフレーム70から派生した合成訓練例です。'
                        'A1=0（試験）と、国A2=103（インドネシア）・国外Vn=0・主楕円を保持し、'
                        '0始まりのビット129〜145（A17/C7〜C10）を変更しています。'
                        'CRCとNMEAチェックサムを再計算しています。')
    synthetic_dcx('dcx-secondary-ellipse', 'DCX / 第2楕円・合成訓練例', 'OutsideJapan', 70,
                  secondary_origin + 'A17=2、C7=2、C8=3（1倍）、C9=8（90度）、C10=30（試験）を指定します。',
                  fields=((129, 2, 2), (131, 2, 2), (133, 3, 3), (136, 5, 8), (141, 5, 30)),
                  source=RECEIVER, tags=('第2楕円', '複数範囲'))
    synthetic_dcx('dcx-secondary-overlap', 'DCX / 第2楕円の重なり・合成訓練例', 'OutsideJapan', 70,
                  secondary_origin + 'A17=2、C7=0、C8=7（2倍）、C9=0、C10=30（試験）を指定します。'
                  '同心の第2楕円が主楕円を完全に含む場合も、重なる部分の主楕円の指示を優先します。',
                  fields=((129, 2, 2), (131, 2, 0), (133, 3, 7), (136, 5, 0), (141, 5, 30)),
                  source=RECEIVER, tags=('第2楕円', '範囲の重なり'))
    synthetic_dcx('dcx-secondary-evacuation', 'DCX / 第2楕円の避難先特例・合成訓練例', 'OutsideJapan', 70,
                  secondary_origin + 'A17=2、C7=2、C8=3（1倍）、C9=8（90度）、C10=0を指定します。'
                  '第2楕円を通報で指定された避難先として表示し、主範囲に対する既定指示を投稿本文で確認します。',
                  fields=((129, 2, 2), (131, 2, 2), (133, 3, 3), (136, 5, 8), (141, 5, 0)),
                  source=RECEIVER, tags=('第2楕円', '避難先特例'))
    synthetic_dcx('dcx-evacuation-overlap', 'DCX / 避難先の完全な重なり・合成訓練例', 'OutsideJapan', 70,
                  secondary_origin + 'A17=2、C7=0、C8=0（0.25倍）、C9=0、C10=0を指定します。'
                  '主楕円と同心で、その内側に収まる避難先の指定を保持します。'
                  '重なる区域では主範囲の指示が優先し、避難先の指定から実際の安全性は推定しません。',
                  fields=((129, 2, 2), (131, 2, 0), (133, 3, 0), (136, 5, 0), (141, 5, 0)),
                  source=RECEIVER, tags=('第2楕円', '避難先特例', '範囲の重なり'))
    synthetic_dcx('dcx-earthquake-vector', 'DCX / B4地震の震央指定・合成訓練例', 'OutsideJapan', 70,
                  '公開受信ログのフレーム70から派生した合成訓練例です。'
                  'A1=0（試験）と、国A2=103（インドネシア）・国外Vn=0・主楕円・日時を保持し、'
                  '0始まりのビット40〜46（A4）と129〜145（A17/D1〜D4）を変更しています。'
                  'CRCとNMEAチェックサムを再計算しています。'
                  'A4=36（地震）、A17=3（B4）、D1=5、D2=3、D3=0（0°）、D4=0を指定します。'
                  'D4は生値0でも主半長軸の0.25倍で、震央位置の指定なしではありません。'
                  '方位基準と地点への変換方法が未確認のため、主楕円だけを描かずトピック表示へ戻します。',
                  fields=((40, 7, 36), (129, 2, 3), (131, 4, 5), (135, 3, 3),
                          (138, 4, 0), (142, 4, 0)),
                  source=RECEIVER, tags=('B4地震', '震央位置', '未確認'))
    synthetic_dcx('dcx-municipality', 'DCX / 市町村の対象区域', 'LAlert', 0,
                  '公式アーカイブのフレーム0から派生した合成訓練例です。'
                  'Vn=1、A1=0（試験）、EX1=1202（函館市）に設定し、'
                  'CRCとNMEAチェックサムを再計算しています。',
                  fields=((214, 6, 1), (146, 16, 1202)))
    synthetic_dcx('dcx-ward', 'DCX / 札幌市中央区の対象区域', 'LAlert', 0,
                  '公式アーカイブのフレーム0から派生した合成訓練例です。'
                  'Vn=1、A1=0（試験）に設定し、EX1=1101（札幌市中央区）は保持しています。'
                  'CRCとNMEAチェックサムを再計算しています。',
                  fields=((214, 6, 1),), tags=('政令市の区',))
    synthetic_dcx('dcx-wards', 'DCX / 札幌市中央区・北区の対象区域', 'JAlert', 115,
                  '公式アーカイブのフレーム115から派生した合成訓練例です。'
                  'Vn=1、A1=0（試験）、EX8=1、EX9を札幌市中央区・北区の指定に設定し、'
                  'CRCとNMEAチェックサムを再計算しています。',
                  fields=((214, 6, 1), (146, 1, 1), (147, 64, (1101 << 48) | (1102 << 32))),
                  tags=('政令市の区', '複数区域'))
    synthetic_dcx('dcx-prefectures', 'DCX / 都道府県の対象区域', 'JAlert', 115,
                  '公式アーカイブのフレーム115から派生した合成訓練例です。'
                  'Vn=1、A1=0（試験）、EX9を北海道・青森県・岩手県のみの指定に設定し、'
                  'CRCとNMEAチェックサムを再計算しています。',
                  # EX9 has 47 prefecture bits followed by 17 reserved bits.
                  fields=((214, 6, 1), (147, 64, 7 << 17)), tags=('複数区域',))
    synthetic_dcx('dcx-ellipse', 'DCX / 受信した楕円範囲', 'MTInfo', 163,
                  '公式アーカイブのフレーム163から派生した合成訓練例です。'
                  'Vn=1、A1=0（試験）、EX2〜EX7を0（追加の避難方向・楕円なし）に設定し、'
                  'CRCとNMEAチェックサムを再計算しています。基本楕円の値は保持しています。',
                  fields=((214, 6, 1), (162, 52, 0)))
    mtinfo_origin = ('公式アーカイブのフレーム163から派生した合成訓練例です。'
                     'A1=0（試験）、Vn=1（0始まりのビット214〜219）に設定し、'
                     '日時・EX1=1202（函館市）・追加楕円EX3〜EX7を保持しています。'
                     'CRCとNMEAチェックサムを再計算しています。')
    synthetic_dcx('dcx-mtinfo-leave', 'DCX / 追加範囲から離れる指示・合成訓練例', 'MTInfo', 163,
                  mtinfo_origin + '主楕円とEX2=0を保持し、通報対象範囲と離れる範囲を区別します。'
                  '範囲外を安全な区域として補いません。',
                  fields=((214, 6, 1),), tags=('追加避難範囲', '範囲から離れる'))
    synthetic_dcx('dcx-mtinfo-head', 'DCX / 追加範囲へ向かう指示・合成訓練例', 'MTInfo', 163,
                  mtinfo_origin + '主楕円を保持し、EX2=1（ビット162）に変更しています。'
                  '通報が向かう先として指定した範囲を表示し、安全性や経路を補いません。',
                  fields=((214, 6, 1), (162, 1, 1)), tags=('追加避難範囲', '範囲へ向かう'))
    synthetic_dcx('dcx-mtinfo-area', 'DCX / 市町村と追加避難範囲・合成訓練例', 'MTInfo', 163,
                  mtinfo_origin + 'A12〜A16（ビット80〜128）とA18（ビット131〜145）を0にし、'
                  'EX2=1（ビット162）に変更しています。'
                  '通報対象の函館市境界と向かう先の追加楕円を、両方を保った地図で確認します。',
                  fields=((214, 6, 1), (80, 49, 0), (131, 15, 0), (162, 1, 1)),
                  tags=('追加避難範囲', '市町村', '範囲へ向かう'))
    synthetic('weather-cancel', '気象 / 取消の表示', 'Weather', 'weather',
              '合成訓練例の情報形態を取消へ変更し、CRCとNMEAチェックサムを再計算しています。',
              fields=((41, 2, 2),), state='cancel')
    synthetic('seismic-islands', '震度情報 / 東京都と離島', 'SeismicIntensity', 'seismic_intensity',
              '合成訓練例の区域を東京都だけに変更しています。島を含む県形状と拡大位置を確認します。',
              fields=((69, 144, 0), (69, 3, 3), (72, 6, 13)), tags=('離島',))
    recorded('weather-many', '気象 / 7区域・複数種別', 'Weather', (53, 54, 55),
             '同じ発表の3電文に含まれる7区域と複数の警報種別をまとめます。',
             tags=('多数区域', '複数電文'))

    def synthetic_cancel(slug, title, category, filename, description, *, original_fields=(),
                         cancel_fields=(), reference=False, tags=()):
        file = Path('image_service/examples') / (filename + '.json')
        raw = (root / file).read_bytes()
        rows = json.loads(raw)
        original = tuple((_training_variant(row['nmea'], ((41, 2, 0), *original_fields)),
                          row['received_ms']) for row in rows)
        # The first frame is the explicit synthetic cancellation. The
        # reference preserves every original forecast in its known bundle.
        nmea, ms = original[0]
        cancelled = ((_training_variant(nmea, ((41, 2, 2), *cancel_fields)), ms),)
        cases.append(_validated(GalleryCase(
            slug, title, category, 'synthetic', 'training', 'cancel', description,
            '同梱例から派生した合成訓練例（実受信の取消ではありません）', None, file.as_posix(),
            cancelled, tuple(tags), hashlib.sha256(raw).hexdigest(),
            cancel_reference=original if reference else ())))

    weather_cancel_origin = (
        '同梱weather.jsonから派生した合成訓練例で、実受信の取消ではありません。'
        '日時・区域コードを保持し、0始まりのビット14〜16をRc=7（訓練）、'
        '41〜42をIt=2（取消）、56・80・104・128・152から始まる各5bitを'
        'Ww=21（記録的短時間大雨情報）に設定しています。'
        'CRC-24QとNMEAチェックサムを再計算しています。')
    weather_fields = tuple((56 + 24 * index, 5, 21) for index in range(5))
    synthetic_cancel('weather-cancel-area', '取消 / 取消報の地域・合成訓練例', 'Weather', 'weather',
                     weather_cancel_origin + '元通報の参照は与えず、取消報に記載された区域だけで背景地図を作ります。',
                     original_fields=weather_fields, tags=('取消', '区域の背景'))
    synthetic_cancel('weather-cancel-reference', '取消 / 元情報の画角・合成訓練例', 'Weather', 'weather',
                     weather_cancel_origin + '同じWw・区域・日時のIt=0訓練電文を参照用に生成し、'
                     '作成時に分かっている対応を明示的に与えます。自動照合の実証ではありません。',
                     original_fields=weather_fields, reference=True, tags=('取消', '明示した参照', '元情報の画角'))
    synthetic_cancel('weather-cancel-unknown', '取消 / 未知区域で地図なし・合成訓練例', 'Weather', 'weather',
                     weather_cancel_origin + 'さらに取消報のPl_1（ビット61〜79）を未定義の524287へ変更します。'
                     '他の既知区域だけの地図を残さず、地図なしの取消カードへ戻る例です。',
                     original_fields=weather_fields, cancel_fields=((61, 19, 524287),),
                     tags=('取消', '未知区域', '地図なし'))
    synthetic_cancel('typhoon-cancel-reference', '取消 / 台風の元の全画角・合成訓練例', 'Typhoon', 'typhoon',
                     '同梱typhoon.jsonの6電文から派生した合成訓練例で、実受信の取消ではありません。'
                     '全電文の0始まりのビット14〜16をRc=7（訓練）、41〜42をIt=0（発表）に設定し、'
                     '日時・台風番号・実況予報6時点は保持します。先頭電文だけIt=2（取消）へ変更し、'
                     '元の6電文との作成時に分かっている対応を明示的に与えます。'
                     'CRC-24QとNMEAチェックサムを再計算しています。'
                     '自動照合の実証ではなく、元の集約全体の画角を背景に再現する例です。',
                     reference=True, tags=('取消', '明示した参照', '複数電文', '元情報の画角'))
    return tuple(cases)
