#!/usr/bin/env python3
"""Render offline reception timelines with real collectors and preview images."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.gallery_posts import WAIT_SECONDS
from scripts.gallery_timeline import plan_timeline
from scripts.render_gallery import _preflight, _save_image, _source_info, _write_json
from scripts.timeline_catalog import load_scenarios
from scripts.timeline_html import render_html


def generate(output, *, selected_scenarios=None):
    """Write a new, self-contained timeline; never overwrite saved evidence."""
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'既存の出力は上書きしません: {output}')
    scenarios = load_scenarios()
    if selected_scenarios is not None:
        selected = set(selected_scenarios)
        if not selected or selected - {case.slug for case in scenarios}:
            raise ValueError('不明または空のシナリオ指定です')
        scenarios = tuple(case for case in scenarios if case.slug in selected)
    # Finish planning and validate assets before creating output.
    plans = {case.slug: plan_timeline(case.records, case.arrival_seconds,
                                     until_seconds=case.until_seconds)
             for case in scenarios}
    _preflight('v4', preview_only=True)
    license_text = (ROOT / 'tests/fixtures/qzqsm/LICENSE.azarashi').read_text(encoding='utf-8')
    output.mkdir(parents=True, exist_ok=False)
    (output / 'images').mkdir()
    (output / 'records').mkdir()
    (output / 'LICENSE.azarashi').write_text(license_text, encoding='utf-8')
    manifest = {
        'schema_version': 1, 'kind': 'reception-timeline',
        'source': _source_info(), 'error_count': 0,
        'conditions': {
            'clock': 'synthetic_elapsed_seconds', 'history': 'empty',
            'delivery': 'immediate_success', 'collection_wait_seconds': WAIT_SECONDS,
            'publisher_idle_seconds': 1,
            'same_time_order': 'timer_before_receipt_then_wake_on_accept',
            'image_version': 'preview', 'nankai_topic_images': True,
        }, 'scenarios': [],
    }
    cache = {}
    for case in scenarios:
        row = asdict(case)
        row['record_count'] = len(case.records)
        row['records_path'] = f'records/{case.slug}.json'
        _write_json(output / row['records_path'], {
            'records': case.records, 'arrival_seconds': case.arrival_seconds,
            'until_seconds': case.until_seconds, 'receipt_labels': case.receipt_labels,
            'sources': case.sources,
        })
        row['steps'] = []
        number = 0
        for step in plans[case.slug]:
            value = asdict(step)
            value['posts'] = []
            for post in step.posts:
                number += 1
                item = asdict(post)
                item.update(number=number, record_count=len(post.record_indexes), images={})
                if post.image_enabled:
                    entries = tuple(case.records[i] for i in post.image_record_indexes)
                    if entries not in cache:
                        cache[entries] = _save_image(output, f'{case.slug}-{number}', entries, 'preview')
                    item['images']['preview'] = cache[entries]
                    manifest['error_count'] += int(bool(cache[entries].get('error')))
                value['posts'].append(item)
            row['steps'].append(value)
        row['post_count'] = number
        manifest['scenarios'].append(row)
    # JSON-shaped data is shared by the saved evidence and HTML rendering.
    manifest = json.loads(json.dumps(manifest, ensure_ascii=False))
    _write_json(output / 'manifest.json', manifest)
    (output / 'index.html').write_text(render_html(manifest), encoding='utf-8')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='新しい出力ディレクトリ')
    parser.add_argument('--scenario', action='append', help='生成するシナリオ（複数指定可）')
    parser.add_argument('--list', action='store_true', help='描画せずシナリオを表示')
    args = parser.parse_args(argv)
    if args.list:
        for case in load_scenarios():
            print(f'{case.slug}\t{case.title}')
        return 0
    if args.output is None:
        parser.error('--output が必要です')
    try:
        result = generate(args.output, selected_scenarios=args.scenario)
    except (ValueError, OSError, ImportError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({'output': str(args.output.absolute()), 'scenarios': len(result['scenarios']),
                      'posts': sum(row['post_count'] for row in result['scenarios']),
                      'errors': result['error_count']}, ensure_ascii=False))
    return 1 if result['error_count'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
