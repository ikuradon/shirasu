#!/usr/bin/env python3
"""Build an offline HTML/PNG comparison of recorded and synthetic QZQSM cases."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import re
import struct
import sys

# Support both `python scripts/render_gallery.py` and module imports in tests.
ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.gallery_catalog import load_cases
from scripts.gallery_html import render_html


VERSIONS = ('v1', 'v2', 'v3', 'v4')
MODULES = {version: 'image_service.render' + ('' if version == 'v1' else '_' + version)
           for version in VERSIONS}
MODULES['preview'] = 'image_service.render_preview'


def _preflight(published_version, *, preview_only=False):
    """Fail before creating output if the offline renderer cannot run."""
    flood = ROOT / 'image_service/assets/preview/flood.geojson.zlib'
    if not flood.is_file():
        raise FileNotFoundError(
            '洪水用データがありません。python scripts/prepare_flood_assets.py を先に実行してください。')
    for version in (('preview',) if preview_only else (published_version, 'preview')):
        importlib.import_module(MODULES[version]).verify_assets()


def _render_with_decision(records, version, *, cancel_reference=None):
    """Observe the actual saved figure without changing frozen renderer output."""
    from unittest.mock import patch
    from image_service import render as base

    renderer = importlib.import_module(MODULES[version])
    kinds = []
    original = base.Figure.savefig

    def observe(figure, *args, **kwargs):
        result = original(figure, *args, **kwargs)
        if any(text.get_gid() == 'dcx-topic' for text in figure.texts):
            kinds.append('topic')
        else:
            kinds.append('map' if figure.axes else 'summary')
        return result

    # The observer is process-local, restored on failure, and shares the renderer
    # lock. In particular, Figure.clear and fallback drawing are left intact.
    with base._LOCK, patch.object(base.Figure, 'savefig', observe):
        options = {'cancel_reference': cancel_reference} if version == 'preview' and cancel_reference else {}
        image = renderer.render(records, **options)
    if len(kinds) != 1:
        raise ValueError('描画結果を確認できませんでした')
    decision = getattr(image, 'decision', None) if version == 'preview' else None
    if decision is not None:
        observed = {'geometry': 'map', 'area': 'map', 'map': 'map',
                    'summary': 'summary', 'topic': 'topic'}
        if observed.get(decision.mode) != kinds[0]:
            raise ValueError('描画結果と表示判断の記録が一致しません')
    return image.png, kinds[0], decision.as_dict() if decision is not None else None


def _render_image(records, version):
    """Retain the PNG/kind helper used by offline scripts."""
    png, kind, _ = _render_with_decision(records, version)
    return png, kind


def _decoded_text(records):
    import azarashi
    return '\n\n'.join(str(azarashi.decode(nmea, timestamp=datetime.fromtimestamp(
        milliseconds / 1000, timezone.utc))) for nmea, milliseconds in records)


def _post_text(records):
    """Show supplemented instruction posts without inventing grouped posts."""
    if len(records) != 1:
        return None
    import azarashi
    from dcx_instructions import MtInfoEvacuationError, is_evacuation_destination, mtinfo_evacuation
    from message_format import report_message

    nmea, milliseconds = records[0]
    report = azarashi.decode(nmea, timestamp=datetime.fromtimestamp(milliseconds / 1000, timezone.utc))
    try:
        additional = mtinfo_evacuation(report)
    except MtInfoEvacuationError:
        additional = None
    return report_message(report) if is_evacuation_destination(report) or additional is not None else None


def _write_json(path, value):
    with path.open('x', encoding='utf-8') as destination:
        json.dump(value, destination, ensure_ascii=False, indent=2)
        destination.write('\n')


def _select_cases(selected_cases):
    cases = load_cases()
    slugs = [case.slug for case in cases]
    if len(set(slugs)) != len(slugs) or any(
            not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug) for slug in slugs):
        raise ValueError('ギャラリーのサンプルIDが不正です')
    if selected_cases is None:
        return cases
    requested = set(selected_cases)
    if not requested:
        raise ValueError('サンプルを1件以上指定してください')
    unknown = requested - set(slugs)
    if unknown:
        raise ValueError('不明なサンプルID: ' + ', '.join(sorted(unknown)))
    return tuple(case for case in cases if case.slug in requested)


def generate(output, *, published_version='v4', selected_cases=None, preview_only=False):
    """Write a fresh gallery; retain and report individual rendering failures.

    No current date, live receiver, private configuration, signing or network is
    used. The same inputs and dependencies produce the same manifest and HTML.
    """
    if published_version not in VERSIONS:
        raise ValueError('公開済み版は v1 / v2 / v3 / v4 から選んでください')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'既存の出力は上書きしません: {output}')
    cases = _select_cases(selected_cases)
    _preflight(published_version, preview_only=preview_only)
    license_text = (ROOT / 'tests/fixtures/qzqsm/LICENSE.azarashi').read_text(encoding='utf-8')
    texts = {case.slug: _decoded_text(case.records) for case in cases}
    posts = {case.slug: _post_text(case.records) for case in cases}
    output.mkdir(parents=True, exist_ok=False)
    (output / 'images').mkdir()
    (output / 'records').mkdir()
    with (output / 'LICENSE.azarashi').open('x', encoding='utf-8') as destination:
        destination.write(license_text)
    manifest = {'schema_version': 1, 'source_license_path': 'LICENSE.azarashi',
                'error_count': 0, 'cases': []}
    if preview_only:
        manifest['renderer'] = 'preview'
    else:
        manifest['published_version'] = published_version
    slots = (('preview', 'preview'),) if preview_only else (
        ('published', published_version), ('preview', 'preview'))
    for index, case in enumerate(cases, 1):
        row = asdict(case)
        del row['records']
        row.pop('cancel_reference', None)
        row['tags'] = list(case.tags)
        row.update(record_count=len(case.records), records_path=f'records/{case.slug}.json',
                   reference_times=sorted({datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
                                           .isoformat() for _, milliseconds in case.records}),
                   report_text=texts[case.slug], images={})
        if posts[case.slug] is not None:
            row['post_text'] = posts[case.slug]
        _write_json(output / row['records_path'],
                    [{'nmea': nmea, 'received_ms': milliseconds}
                     for nmea, milliseconds in case.records])
        if case.cancel_reference:
            row['cancel_reference_path'] = f'records/{case.slug}-reference.json'
            row['cancel_reference_note'] = '合成元が明らかな訓練例の参照。実受信通報の自動照合ではありません。'
            _write_json(output / row['cancel_reference_path'],
                        [{'nmea': nmea, 'received_ms': milliseconds}
                         for nmea, milliseconds in case.cancel_reference])
        for slot, version in slots:
            print(f'[{index}/{len(cases)}] {case.slug} / {version}', flush=True)
            try:
                options = {'cancel_reference': case.cancel_reference} if version == 'preview' and case.cancel_reference else {}
                png, kind, decision = _render_with_decision(case.records, version, **options)
                if len(png) < 24 or png[:8] != b'\x89PNG\r\n\x1a\n' or kind not in ('map', 'summary', 'topic'):
                    raise ValueError('不正な描画結果です')
                width, height = struct.unpack('>II', png[16:24])
                if not width or not height:
                    raise ValueError('PNGの寸法が不正です')
                name = case.slug if preview_only else f'{case.slug}-{version}'
                relative = f'images/{name}.png'
                with (output / relative).open('xb') as destination:
                    destination.write(png)
                row['images'][slot] = {'path': relative, 'version': version, 'kind': kind,
                                       'sha256': hashlib.sha256(png).hexdigest(),
                                       'width': width, 'height': height}
                if version == 'preview' and decision is not None:
                    row['images'][slot]['decision'] = decision
            except Exception as error:
                # A failed cell remains visible; the command's exit status also
                # fails. Never relabel a failure as a successful summary card.
                message = ' '.join(str(error).split())[:400]
                row['images'][slot] = {'version': version,
                                       'error': f'{type(error).__name__}: {message}'}
                manifest['error_count'] += 1
        manifest['cases'].append(row)
    _write_json(output / 'manifest.json', manifest)
    with (output / 'index.html').open('x', encoding='utf-8') as destination:
        destination.write(render_html(manifest))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='新しく作成するギャラリーのディレクトリ')
    parser.add_argument('--published-version', choices=VERSIONS, default='v4',
                        help='比較する公開済み版（既定: v4。稼働中の設定とは独立）')
    parser.add_argument('--preview-only', action='store_true',
                        help='作業版のみを生成し、公開済み版との比較を省く')
    parser.add_argument('--case', action='append', dest='cases', metavar='SLUG',
                        help='生成するサンプルID。繰り返し指定可。省略時は全件')
    parser.add_argument('--list', action='store_true', help='描画せずサンプルIDを一覧表示')
    args = parser.parse_args(argv)
    if args.list and (args.output is not None or args.cases is not None or args.preview_only):
        parser.error('--list と --output / --case / --preview-only は同時に指定できません')
    if not args.list and args.output is None:
        parser.error('--output が必要です')
    try:
        if args.list:
            for case in load_cases():
                print(f'{case.slug}\t{case.title}\t{case.source_kind}\t{case.classification}\t{case.state}')
            return 0
        result = generate(args.output, published_version=args.published_version,
                          selected_cases=None if args.cases is None else tuple(args.cases),
                          preview_only=args.preview_only)
    except (OSError, ValueError, ImportError) as error:
        print(f'ギャラリーを作成できません: {error}', file=sys.stderr)
        return 1
    print(f"{len(result['cases'])}件 / 描画失敗 {result['error_count']}件: "
          f"{args.output.absolute() / 'index.html'}")
    return 1 if result['error_count'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
