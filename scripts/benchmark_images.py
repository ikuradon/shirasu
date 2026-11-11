#!/usr/bin/env python3
"""Measure unpublished renderers and image codecs in isolated serial workers."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))


def distribution(values):
    return {'samples': values, 'median': statistics.median(values),
            'min': min(values), 'max': max(values)}


def write_csv(output, results):
    fields = ('image', 'category', 'format', 'cold_import_ms', 'cold_render_ms',
              'warm_render_median_ms', 'warm_png_save_median_ms', 'peak_rss_mib',
              'bytes', 'encode_median_ms', 'decode_median_ms', 'pixel_exact', 'psnr_rgb_db', 'error')
    with (output / 'results.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in results:
            for variant in row.get('formats') or [{}]:
                writer.writerow({
                    'image': row['id'], 'category': row['category'], 'format': variant.get('id'),
                    'cold_import_ms': row.get('cold', {}).get('import_ms'),
                    'cold_render_ms': row.get('cold', {}).get('render_ms'),
                    'warm_render_median_ms': row.get('warm', {}).get('render_ms', {}).get('median'),
                    'warm_png_save_median_ms': row.get('warm', {}).get('png_save_ms', {}).get('median'),
                    'peak_rss_mib': row.get('peak_rss_mib'), 'bytes': variant.get('bytes'),
                    'encode_median_ms': (variant.get('encode_ms') or {}).get('median'),
                    'decode_median_ms': (variant.get('decode_ms') or {}).get('median'),
                    'pixel_exact': variant.get('pixel_exact'), 'psnr_rgb_db': variant.get('psnr_rgb_db'),
                    'error': row.get('error') or variant.get('error'),
                })


def workloads(selected_cases=None):
    """Use the same raw and actual-post inputs as the approved gallery."""
    from scripts.gallery_posts import plan_posts
    from scripts.render_gallery import _select_cases
    items, seen = [], {}
    for case in _select_cases(selected_cases):
        posts = plan_posts(case.records, nankai_topics=True)
        indexes = (posts[0].image_record_indexes if case.category == 'NankaiTroughEarthquake'
                   else tuple(range(len(case.records))))
        bundles = [('case', None, tuple(case.records[i] for i in indexes), case.cancel_reference)]
        bundles += [('post', number, tuple(case.records[i] for i in post.image_record_indexes), ())
                    for number, post in enumerate(posts, 1) if post.image_enabled]
        for role, number, records, reference in bundles:
            key = records, reference
            usage = {'case_slug': case.slug, 'role': role}
            if number is not None:
                usage['post_number'] = number
            if key in seen:
                seen[key]['usages'].append(usage)
                continue
            identifier = case.slug if role == 'case' else f'{case.slug}-post-{number:02d}'
            item = {'id': identifier, 'title': case.title, 'category': case.category,
                    'usages': [usage], 'records': records, 'cancel_reference': reference,
                    'record_count': len(records), 'sources': {
                        'kind': case.source_kind, 'file': case.source_file,
                        'url': case.source_url, 'sha256': case.source_sha256}}
            items.append(item)
            seen[key] = item
    return items


def environment():
    from importlib.metadata import version
    from PIL import features
    data = {'python': platform.python_version(), 'system': platform.system(),
            'machine': platform.machine(), 'logical_cpus': os.cpu_count(),
            'packages': {name: version(name) for name in ('azarashi', 'matplotlib', 'numpy', 'Pillow')},
            'codecs': {name: features.check(name) for name in ('webp', 'avif', 'jpg', 'zlib')},
            'codec_versions': {name: features.version(name) for name in ('webp', 'avif', 'jpg', 'zlib')}}
    # Public execution constraints only; never record the process environment.
    for name in ('cpu.max', 'memory.max'):
        path = Path('/sys/fs/cgroup') / name
        if path.is_file():
            data[name] = path.read_text().strip()
    if hasattr(os, 'sched_getaffinity'):
        data['affinity_cpus'] = len(os.sched_getaffinity(0))
    return data


def worker(payload, output, *, warm_repeats, encoding_repeats):
    """A fresh process: imports and first render must not be prewarmed."""
    import resource
    from unittest.mock import patch

    data = json.loads(Path(payload).read_text())
    entries = tuple(tuple(record) for record in data['records'])
    reference = tuple(tuple(record) for record in data['cancel_reference'])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    renderer = importlib.import_module('image_service.render_preview')
    import_ms = (time.perf_counter() - started) * 1000
    from PIL import Image
    png_times = []
    original_save = Image.Image.save

    def timed_save(image, fp, format=None, **parameters):
        before = time.perf_counter()
        try:
            return original_save(image, fp, format=format, **parameters)
        finally:
            if str(format).upper() == 'PNG':
                png_times.append((time.perf_counter() - before) * 1000)

    durations, encodings, hashes = [], [], []
    with patch.object(Image.Image, 'save', timed_save):
        for _ in range(1 + warm_repeats):
            png_times.clear()
            before = time.perf_counter()
            image = renderer.render(entries, **({'cancel_reference': reference} if reference else {}))
            durations.append((time.perf_counter() - before) * 1000)
            encodings.append(sum(png_times))
            hashes.append(hashlib.sha256(image.png).hexdigest())
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mib = rss / (1024 * 1024 if sys.platform == 'darwin' else 1024)
    # Transcoding follows rendering, so it cannot warm the measured renderer.
    from scripts.image_encoding_benchmark import evaluate
    formats = evaluate(image.png, output, repeats=encoding_repeats)
    result = {
        'cold': {'import_ms': import_ms, 'render_ms': durations[0],
                 'first_ready_ms': import_ms + durations[0], 'png_save_ms': encodings[0]},
        'warm': {'render_ms': distribution(durations[1:]),
                 'png_save_ms': distribution(encodings[1:])},
        'stable_png': len(set(hashes)) == 1, 'render_sha256': hashes,
        'peak_rss_mib': rss_mib, 'formats': formats, 'environment': environment(),
    }
    (output / 'worker.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return result


def generate(output, *, selected_cases=None, warm_repeats=3, encoding_repeats=3, timeout=300):
    for label, value in (('warm_repeats', warm_repeats), ('encoding_repeats', encoding_repeats)):
        if type(value) is not int or not 1 <= value <= 10:
            raise ValueError(f'{label} must be between 1 and 10')
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError('timeout must be between 1 and 3600 seconds')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'既存の出力は上書きしません: {output}')
    inputs = workloads(selected_cases)
    from scripts.render_gallery import _source_info
    from scripts.benchmark_html import render_html
    source = _source_info()
    output.mkdir(parents=True, exist_ok=False)
    (output / 'records').mkdir()
    (output / 'images').mkdir()
    (output / 'LICENSE.azarashi').write_bytes((ROOT / 'tests/fixtures/qzqsm/LICENSE.azarashi').read_bytes())
    manifest = {
        'schema_version': 1, 'kind': 'render-benchmark', 'source': source,
        'created_at': datetime.now(timezone.utc).isoformat(), 'environment': {}, 'error_count': 0,
        'codec_error_count': 0,
        'settings': {'renderer': 'preview', 'warm_repeats': warm_repeats,
                     'encoding_repeats': encoding_repeats, 'units': 'ms',
                     'cold_scope': 'fresh_process_and_font_cache; OS_page_cache_not_flushed',
                     'timing_scope': 'direct_render_including_PNG; no_HTTP_or_CDN_cache',
                     'first_ready_scope': 'renderer_import_plus_first_render; excludes_interpreter_startup',
                     'format_scope': 'post_render_transcode; source_PNG_generation_is_additional',
                     'order': 'serial_in_catalog_order', 'timeout_seconds': timeout},
        'results': [],
    }
    for index, item in enumerate(inputs, 1):
        print(f'[{index}/{len(inputs)}] {item["id"]}', flush=True)
        record_path = output / 'records' / (item['id'] + '.json')
        record_path.write_text(json.dumps(item, ensure_ascii=False, indent=2))
        row = {key: value for key, value in item.items() if key not in ('records', 'cancel_reference')}
        row['records_path'] = str(record_path.relative_to(output))
        target = output / 'images' / item['id']
        try:
            with tempfile.TemporaryDirectory(prefix='shirasu-benchmark-fonts-') as font_cache:
                env = dict(os.environ, MPLCONFIGDIR=font_cache)
                subprocess.run(
                    [sys.executable, '-B', str(Path(__file__).resolve()), '--worker', str(record_path),
                     '--output', str(target), '--warm-repeats', str(warm_repeats),
                     '--encoding-repeats', str(encoding_repeats)],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=True)
            result = json.loads((target / 'worker.json').read_text())
            manifest['environment'] = result.pop('environment')
            row.update(result)
            for image in row['formats']:
                if image.get('path'):
                    image['path'] = str(Path('images') / item['id'] / image['path'])
                manifest['codec_error_count'] += int(bool(image.get('error')))
            if not row['stable_png']:
                raise ValueError('Repeated rendering changed PNG bytes')
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
            row['error'] = f'{type(error).__name__}: {str(detail)[-600:]}'
            manifest['error_count'] += 1
        manifest['results'].append(row)
        # Checkpoint only inside this newly created run, including failed rows.
        (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False))
    write_csv(output, manifest['results'])
    (output / 'index.html').write_text(render_html(manifest), encoding='utf-8')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--case', action='append')
    parser.add_argument('--warm-repeats', type=int, default=3)
    parser.add_argument('--encoding-repeats', type=int, default=3)
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.list:
        for item in workloads(args.case):
            print(f'{item["id"]}\t{item["title"]}\t{len(item["usages"])} uses')
        return 0
    if args.output is None:
        parser.error('--output が必要です')
    try:
        if args.worker:
            worker(args.worker, args.output, warm_repeats=args.warm_repeats,
                   encoding_repeats=args.encoding_repeats)
            return 0
        result = generate(args.output, selected_cases=args.case, warm_repeats=args.warm_repeats,
                          encoding_repeats=args.encoding_repeats, timeout=args.timeout)
    except (ValueError, OSError, ImportError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({'images': len(result['results']), 'errors': result['error_count'],
                      'codec_errors': result['codec_error_count'],
                      'output': str(args.output.absolute())}, ensure_ascii=False))
    return int(bool(result['error_count'] or result['codec_error_count']))


if __name__ == '__main__':
    raise SystemExit(main())
