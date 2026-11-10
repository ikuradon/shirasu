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
    return case


def _training_variant(nmea, fields):
    """Change explicit DCR fields and rebuild CRC-24Q and the NMEA checksum."""
    _, satellite, encoded = nmea.split('*')[0].split(',')
    value = int(encoded + '0', 16)
    for offset, width, replacement in (*fields, (14, 3, 7)):
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
    recorded('dcx-lalert', 'DCX / Lアラート', 'LAlert', (0,),
             '公式アーカイブ由来の試験電文です。現在は要旨表示で確認します。', source=DCX)
    recorded('dcx-jalert', 'DCX / Jアラート', 'JAlert', (115,),
             '公式アーカイブ由来の試験電文です。現在は要旨表示で確認します。', source=DCX)
    recorded('dcx-mtinfo', 'DCX / 自治体情報', 'MTInfo', (163,),
             '公式アーカイブ由来の試験電文です。現在は要旨表示で確認します。', source=DCX)
    recorded('dcx-outside-japan', 'DCX / 海外通報', 'OutsideJapan', (70,),
             '受信ログに含まれる海外向けの試験電文です。現在は要旨表示で確認します。')
    synthetic('weather-cancel', '気象 / 取消の表示', 'Weather', 'weather',
              '合成訓練例の情報形態を取消へ変更し、CRCとNMEAチェックサムを再計算しています。',
              fields=((41, 2, 2),), state='cancel')
    synthetic('seismic-islands', '震度情報 / 東京都と離島', 'SeismicIntensity', 'seismic_intensity',
              '合成訓練例の区域を東京都だけに変更しています。島を含む県形状と拡大位置を確認します。',
              fields=((69, 144, 0), (69, 3, 3), (72, 6, 13)), tags=('離島',))
    recorded('weather-many', '気象 / 7区域・複数種別', 'Weather', (53, 54, 55),
             '同じ発表の3電文に含まれる7区域と複数の警報種別をまとめます。',
             tags=('多数区域', '複数電文'))
    return tuple(cases)
