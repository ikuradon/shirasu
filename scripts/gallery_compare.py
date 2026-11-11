"""Validate and compare saved offline galleries without rendering or repository I/O.

The baseline is a snapshot of validated bytes, not a directory that is read
again after rendering. Only declared PNGs, typed input JSON, and the gallery's
license file can enter the copied preview snapshot.
"""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import zlib


STATUSES = ('changed', 'unchanged', 'input_changed', 'added', 'unavailable')
_MODES = ('map', 'geometry', 'area', 'topic', 'summary')
_STAGES = ('selection', 'geometry', 'area', 'layout', 'render')
_MAX_ASSET_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_PNG = b'\x89PNG\r\n\x1a\n'


@dataclass(frozen=True)
class Baseline:
    """Validated immutable serialization and file bytes captured by load_baseline."""
    _manifest: bytes
    _inputs: tuple
    _assets: tuple

    @property
    def manifest(self):
        return json.loads(self._manifest)


def _fail(message):
    raise ValueError('比較ギャラリー: ' + message)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('JSONのキーが重複しています')
        result[key] = value
    return result


def _parse(data):
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_object,
                          parse_constant=lambda _: _fail('非有限のJSON数値です'))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('比較ギャラリー: JSONを読み取れません') from error


def _relative(value):
    if (not isinstance(value, str) or len(value) > 512
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', value)
            or any(part in ('', '.', '..') for part in value.split('/'))
            or PurePosixPath(value).is_absolute()):
        _fail('安全な相対ファイルパスが必要です')
    return value


class _Reader:
    def __init__(self, directory):
        self.root = Path(directory).absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            _fail('比較元は実在するディレクトリで指定してください')
        self.root = self.root.resolve()
        self.files, self.roles, self.total = {}, {}, 0

    def read(self, value, role):
        name = _relative(value)
        suffix = PurePosixPath(name).suffix.lower()
        if ((role == 'image' and suffix != '.png')
                or (role in ('manifest', 'records') and suffix != '.json')
                or (role == 'license' and PurePosixPath(name).name != 'LICENSE.azarashi')):
            _fail('宣言された種類とファイル名が一致しません')
        if name in self.roles and self.roles[name] != role:
            _fail('同じファイルが異なる用途を持っています')
        self.roles[name] = role
        if name not in self.files:
            path = self.root
            for part in name.split('/'):
                path = path / part
                if path.is_symlink():
                    _fail('比較元の子シンボリックリンクは使用できません')
            if not path.is_file():
                _fail('参照ファイルが存在しません: ' + name)
            maximum = _MAX_ASSET_BYTES if role == 'image' else 8 * 1024 * 1024
            try:
                with path.open('rb') as source:
                    data = source.read(maximum + 1)
            except OSError as error:
                raise ValueError('比較ギャラリー: 参照ファイルを読み取れません') from error
            if len(data) > maximum or self.total + len(data) > _MAX_TOTAL_BYTES:
                _fail('比較元のファイルが大きすぎます')
            self.total += len(data)
            self.files[name] = data
        return self.files[name]


def _records(value, reader):
    rows = _parse(reader.read(value, 'records'))
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        _fail('入力JSONには1〜1000件の電文が必要です')
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'nmea', 'received_ms'}:
            _fail('入力JSONの電文形式が不正です')
        sentence, milliseconds = row['nmea'], row['received_ms']
        if (not isinstance(sentence, str)
                or re.fullmatch(r'\$QZQSM,[0-9]{2},[0-9A-Fa-f]{63}\*[0-9A-Fa-f]{2}', sentence) is None
                or type(milliseconds) is not int or not 0 <= milliseconds <= 253370764799999):
            _fail('入力JSONのNMEAまたは日時が不正です')
        checksum = 0
        for char in sentence[1:-3]:
            checksum ^= ord(char)
        if checksum != int(sentence[-2:], 16):
            _fail('入力NMEAのチェックサムが一致しません')
        result.append((sentence, milliseconds))
    return tuple(result)


def _png(data):
    if not data.startswith(_PNG):
        _fail('PNG署名が不正です')
    offset, dimensions, image_data = 8, None, False
    while offset < len(data):
        if offset + 12 > len(data):
            _fail('PNGが途中で切れています')
        length, = struct.unpack('>I', data[offset:offset + 4])
        end = offset + length + 12
        if end > len(data):
            _fail('PNGチャンクが途中で切れています')
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:end - 4]
        crc, = struct.unpack('>I', data[end - 4:end])
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            _fail('PNGチャンクのチェックサムが一致しません')
        if dimensions is None:
            if kind != b'IHDR' or length != 13:
                _fail('PNGヘッダーが不正です')
            dimensions = struct.unpack('>II', payload[:8])
            if min(dimensions) < 1:
                _fail('PNG寸法が不正です')
        elif kind == b'IHDR':
            _fail('PNGヘッダーが重複しています')
        if kind == b'IDAT':
            image_data = True
        if kind == b'IEND':
            if length or end != len(data) or not image_data:
                _fail('PNG終端が不正です')
            return dimensions
        offset = end
    _fail('PNG終端がありません')


def _decision(image):
    if 'decision' not in image:
        return
    value = image['decision']
    if not isinstance(value, dict) or value.get('mode') not in _MODES:
        _fail('表示方法の記録が不正です')
    if 'reasons' in value:
        reasons = value['reasons']
        if not isinstance(reasons, list):
            _fail('表示理由の記録が不正です')
        for item in reasons:
            if (not isinstance(item, dict) or not isinstance(item.get('code'), str)
                    or not item['code'] or item.get('stage') not in _STAGES
                    or not isinstance(item.get('message'), str)
                    or ('detail' in item and not isinstance(item['detail'], str))):
                _fail('表示理由の項目が不正です')


def _images(images, reader):
    if not isinstance(images, dict) or set(images) - {'preview', 'published'}:
        _fail('画像セルの記録が不正です')
    for slot, image in images.items():
        if not isinstance(image, dict) or image.get('version') not in (
                ('preview',) if slot == 'preview' else ('v1', 'v2', 'v3', 'v4')):
            _fail('画像の版が不正です')
        _decision(image)
        if 'error' in image:
            if (not isinstance(image['error'], str) or not image['error']
                    or any(key in image for key in ('path', 'sha256', 'width', 'height', 'kind'))):
                _fail('描画エラーの記録が不正です')
            continue
        if (image.get('kind') not in ('map', 'summary', 'topic')
                or type(image.get('width')) is not int or type(image.get('height')) is not int
                or not isinstance(image.get('sha256'), str)
                or re.fullmatch('[0-9a-f]{64}', image['sha256']) is None):
            _fail('画像のメタ情報が不正です')
        data = reader.read(image.get('path'), 'image')
        if hashlib.sha256(data).hexdigest() != image['sha256']:
            _fail('PNGのSHA-256が一致しません')
        if _png(data) != (image['width'], image['height']):
            _fail('PNGの寸法と記録が一致しません')


def _validate(manifest, reader):
    if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
            or manifest['schema_version'] != 1 or not isinstance(manifest.get('cases'), list)
            or len(manifest['cases']) > 1000):
        _fail('schema_version=1のギャラリーが必要です')
    if ('commit' in manifest and manifest['commit'] is not None
            and (not isinstance(manifest['commit'], str)
                 or re.fullmatch(r'[0-9a-fA-F]{7,40}|[0-9a-fA-F]{64}', manifest['commit']) is None)):
        _fail('コミットの記録が不正です')
    if 'working_tree' in manifest and manifest['working_tree'] is not None and type(manifest['working_tree']) is not bool:
        _fail('作業ツリーの記録が不正です')
    if 'post_preview' in manifest:
        value = manifest['post_preview']
        if (not isinstance(value, dict) or type(value.get('images_enabled')) is not bool
                or not isinstance(value.get('history'), str) or not isinstance(value.get('input_timing'), str)
                or type(value.get('collection_wait_seconds')) is not int or value['collection_wait_seconds'] <= 0):
            _fail('投稿プレビューの前提が不正です')
    if 'source_license_path' in manifest:
        try:
            reader.read(manifest['source_license_path'], 'license').decode('utf-8')
        except UnicodeError as error:
            raise ValueError('比較ギャラリー: ライセンスを読み取れません') from error
    seen, inputs = set(), []
    for case in manifest['cases']:
        if (not isinstance(case, dict) or not isinstance(case.get('slug'), str)
                or re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', case['slug']) is None
                or case['slug'] in seen or not isinstance(case.get('title'), str)
                or not isinstance(case.get('category'), str) or not case['category']):
            _fail('ケースの識別子・種別・見出しが不正または重複しています')
        seen.add(case['slug'])
        records = _records(case.get('records_path'), reader)
        reference = (_records(case['cancel_reference_path'], reader)
                     if 'cancel_reference_path' in case else ())
        if type(case.get('record_count')) is not int or case['record_count'] != len(records):
            _fail('ケースの電文数が一致しません')
        inputs.append((case['slug'], (case['category'], records, reference)))
        _images(case.get('images'), reader)
        if 'posts' in case:
            if not isinstance(case['posts'], list) or len(case['posts']) > 1000:
                _fail('投稿一覧が不正です')
            for post in case['posts']:
                if not isinstance(post, dict):
                    _fail('投稿項目が不正です')
                indexes = post.get('record_indexes')
                if (post.get('kind') not in ('single', 'typhoon', 'nankai')
                        or not isinstance(post.get('text'), str) or type(post.get('image_enabled')) is not bool
                        or not isinstance(indexes, list) or not indexes
                        or any(type(index) is not int or not 0 <= index < len(records) for index in indexes)
                        or len(set(indexes)) != len(indexes)
                        or type(post.get('record_count')) is not int or post['record_count'] != len(indexes)):
                    _fail('投稿単位の記録が不正です')
                _images(post.get('images'), reader)
                if not post['image_enabled'] and post['images']:
                    _fail('本文のみの投稿に画像が指定されています')
    return tuple(inputs)


def load_baseline(directory):
    """Reject broken/unsafe saved galleries before any new output is created."""
    reader = _Reader(directory)
    manifest = _parse(reader.read('manifest.json', 'manifest'))
    inputs = _validate(manifest, reader)
    manifest.pop('baseline', None)
    for case in manifest['cases']:
        case.pop('comparison', None)
    return Baseline(_json_bytes(manifest), inputs,
                    tuple((name, reader.roles[name], data) for name, data in reader.files.items()
                          if reader.roles[name] != 'manifest'))


def _result(status, changes=(), note=''):
    return {'status': status, 'changes': list(dict.fromkeys(changes)), 'note': note}


def _image_changes(current, previous):
    if not current or not previous or 'error' in current or 'error' in previous:
        return None
    changes = []
    if any(current[key] != previous[key] for key in ('sha256', 'width', 'height')):
        changes.append('image')
    if ((current['kind'], current.get('decision', {}).get('mode'))
            != (previous['kind'], previous.get('decision', {}).get('mode'))):
        changes.append('mode')
    def reasons(image):
        value = image.get('decision', {})
        return (tuple(sorted(json.dumps(reason, sort_keys=True, ensure_ascii=False) for reason in value['reasons']))
                if 'reasons' in value else None)
    if reasons(current) != reasons(previous):
        changes.append('reasons')
    return changes


def _compare_posts(current, previous, manifest, old_manifest, *, same_input=True):
    if 'posts' not in current or 'posts' not in previous:
        return _result('unavailable', note='投稿単位の記録がないため比較できません')
    if 'post_preview' not in manifest or 'post_preview' not in old_manifest:
        return _result('unavailable', note='投稿プレビューの前提が記録されていません')
    if not same_input:
        return _result('input_changed', note='通報の入力・順序・参照または種別が異なります')
    if manifest['post_preview'] != old_manifest['post_preview']:
        return _result('input_changed', ('conditions',), '投稿プレビューの前提が異なります')
    structure = lambda posts: [(p['kind'], p['record_indexes'], p['image_enabled']) for p in posts]
    changes = []
    if structure(current['posts']) != structure(previous['posts']):
        changes.append('structure')
    if [p['text'] for p in current['posts']] != [p['text'] for p in previous['posts']]:
        changes.append('text')
    unavailable = False
    for new, old in zip(current['posts'], previous['posts']):
        if structure((new,)) != structure((old,)) or not new['image_enabled']:
            continue
        difference = _image_changes(new['images'].get('preview'), old['images'].get('preview'))
        if difference is None:
            unavailable = True
        else:
            changes.extend(difference)
    if unavailable:
        return _result('unavailable', changes, '一部の添付画像に比較できる作業版がありません')
    return _result('changed' if changes else 'unchanged', changes)


def _snapshot(baseline):
    manifest = baseline.manifest
    assets = {name: (role, data) for name, role, data in baseline._assets}
    copied = {}
    def path(name):
        role, data = assets[name]
        digest = hashlib.sha256(data).hexdigest()
        destination = ('images/' + digest + '.png' if role == 'image' else
                       'records/' + digest + '.json' if role == 'records' else 'LICENSE.azarashi')
        copied[destination] = data
        return destination
    if 'source_license_path' in manifest:
        manifest['source_license_path'] = path(manifest['source_license_path'])
    manifest.pop('published_version', None)
    manifest['renderer'] = 'preview'
    for case in manifest['cases']:
        case['records_path'] = path(case['records_path'])
        if 'cancel_reference_path' in case:
            case['cancel_reference_path'] = path(case['cancel_reference_path'])
        for owner in (case, *case.get('posts', ())):
            images = owner['images']
            owner['images'] = {'preview': images['preview']} if 'preview' in images else {}
            image = owner['images'].get('preview', {})
            if 'path' in image:
                image['path'] = path(image['path'])
    return manifest, copied


def _prefixed(case):
    case = deepcopy(case)
    for key in ('records_path', 'cancel_reference_path'):
        if key in case:
            case[key] = 'baseline/' + case[key]
    for owner in (case, *case.get('posts', ())):
        image = owner['images'].get('preview', {})
        if 'path' in image:
            image['path'] = 'baseline/' + image['path']
    return case


def attach_comparison(manifest, baseline, output):
    """Attach comparison records and copy a standalone previous-preview snapshot."""
    if not isinstance(baseline, Baseline):
        _fail('load_baselineで検証した比較元が必要です')
    reader = _Reader(output)
    current_inputs = dict(_validate(manifest, reader))
    old_inputs = dict(baseline._inputs)
    snapshot, files = _snapshot(baseline)
    previous = {case['slug']: case for case in snapshot['cases']}
    counts = {view: dict.fromkeys(STATUSES, 0) for view in ('images', 'posts')}
    comparisons = {}
    for case in manifest['cases']:
        old = previous.get(case['slug'])
        if old is None:
            images = _result('added', note='前回に同じ識別子のケースがありません')
            posts = deepcopy(images)
        elif current_inputs[case['slug']] != old_inputs[case['slug']]:
            images = _result('input_changed', note='通報の入力・順序・参照または種別が異なります')
            posts = _compare_posts(case, old, manifest, snapshot, same_input=False)
        else:
            changes = _image_changes(case['images'].get('preview'), old['images'].get('preview'))
            images = (_result('unavailable', note='比較できる作業版画像がありません') if changes is None
                      else _result('changed' if changes else 'unchanged', changes))
            posts = _compare_posts(case, old, manifest, snapshot)
        comparisons[case['slug']] = {'images': images, 'posts': posts,
                                     'previous': _prefixed(old) if old is not None else None}
        for view, result in (('images', images), ('posts', posts)):
            counts[view][result['status']] += 1
    destination = reader.root / 'baseline'
    if destination.exists() or destination.is_symlink():
        _fail('比較元のコピー先を上書きできません')
    destination.mkdir()
    for name, data in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
    with (destination / 'manifest.json').open('xb') as stream:
        stream.write(_json_bytes(snapshot))
    manifest['baseline'] = {
        'commit': snapshot.get('commit'), 'working_tree': snapshot.get('working_tree'),
        'manifest_path': 'baseline/manifest.json', 'counts': counts,
        'unmatched_cases': [{'slug': case['slug'], 'title': case['title']}
                            for case in snapshot['cases'] if case['slug'] not in current_inputs],
    }
    for case in manifest['cases']:
        case['comparison'] = comparisons[case['slug']]
    return manifest
