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


def _preflight(published_version):
    """Fail before creating output if the offline renderer cannot run."""
    flood = ROOT / 'image_service/assets/preview/flood.geojson.zlib'
    if not flood.is_file():
        raise FileNotFoundError(
            '洪水用データがありません。python scripts/prepare_flood_assets.py を先に実行してください。')
    for version in (published_version, 'preview'):
        importlib.import_module(MODULES[version]).verify_assets()


def _render_image(records, version):
    """Observe the actual saved figure without changing frozen renderer output."""
    from unittest.mock import patch
    from image_service import render as base

    renderer = importlib.import_module(MODULES[version])
    kinds = []
    original = base.Figure.savefig

    def observe(figure, *args, **kwargs):
        result = original(figure, *args, **kwargs)
        kinds.append('map' if figure.axes else 'summary')
        return result

    # The observer is process-local, restored on failure, and shares the renderer
    # lock. In particular, Figure.clear and fallback drawing are left intact.
    with base._LOCK, patch.object(base.Figure, 'savefig', observe):
        image = renderer.render(records)
    if len(kinds) != 1:
        raise ValueError('描画結果を確認できませんでした')
    return image.png, kinds[0]


def _decoded_text(records):
    import azarashi
    return '\n\n'.join(str(azarashi.decode(nmea, timestamp=datetime.fromtimestamp(
        milliseconds / 1000, timezone.utc))) for nmea, milliseconds in records)


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


def generate(output, *, published_version='v4', selected_cases=None):
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
    _preflight(published_version)
    license_text = (ROOT / 'tests/fixtures/qzqsm/LICENSE.azarashi').read_text(encoding='utf-8')
    texts = {case.slug: _decoded_text(case.records) for case in cases}
    output.mkdir(parents=True, exist_ok=False)
    (output / 'images').mkdir()
    (output / 'records').mkdir()
    with (output / 'LICENSE.azarashi').open('x', encoding='utf-8') as destination:
        destination.write(license_text)
    manifest = {'schema_version': 1, 'published_version': published_version,
                'source_license_path': 'LICENSE.azarashi', 'error_count': 0, 'cases': []}
    for index, case in enumerate(cases, 1):
        row = asdict(case)
        del row['records']
        row['tags'] = list(case.tags)
        row.update(record_count=len(case.records), records_path=f'records/{case.slug}.json',
                   reference_times=sorted({datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
                                           .isoformat() for _, milliseconds in case.records}),
                   report_text=texts[case.slug], images={})
        _write_json(output / row['records_path'],
                    [{'nmea': nmea, 'received_ms': milliseconds}
                     for nmea, milliseconds in case.records])
        for slot, version in (('published', published_version), ('preview', 'preview')):
            print(f'[{index}/{len(cases)}] {case.slug} / {version}', flush=True)
            try:
                png, kind = _render_image(case.records, version)
                if len(png) < 24 or png[:8] != b'\x89PNG\r\n\x1a\n' or kind not in ('map', 'summary'):
                    raise ValueError('不正な描画結果です')
                width, height = struct.unpack('>II', png[16:24])
                if not width or not height:
                    raise ValueError('PNGの寸法が不正です')
                relative = f'images/{case.slug}-{version}.png'
                with (output / relative).open('xb') as destination:
                    destination.write(png)
                row['images'][slot] = {'path': relative, 'version': version, 'kind': kind,
                                       'sha256': hashlib.sha256(png).hexdigest(),
                                       'width': width, 'height': height}
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
    parser.add_argument('--case', action='append', dest='cases', metavar='SLUG',
                        help='生成するサンプルID。繰り返し指定可。省略時は全件')
    parser.add_argument('--list', action='store_true', help='描画せずサンプルIDを一覧表示')
    args = parser.parse_args(argv)
    if args.list and (args.output is not None or args.cases is not None):
        parser.error('--list と --output / --case は同時に指定できません')
    if not args.list and args.output is None:
        parser.error('--output が必要です')
    try:
        if args.list:
            for case in load_cases():
                print(f'{case.slug}\t{case.title}\t{case.source_kind}\t{case.classification}\t{case.state}')
            return 0
        result = generate(args.output, published_version=args.published_version,
                          selected_cases=None if args.cases is None else tuple(args.cases))
    except (OSError, ValueError, ImportError) as error:
        print(f'ギャラリーを作成できません: {error}', file=sys.stderr)
        return 1
    print(f"{len(result['cases'])}件 / 描画失敗 {result['error_count']}件: "
          f"{args.output.absolute() / 'index.html'}")
    return 1 if result['error_count'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
