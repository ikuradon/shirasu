#!/usr/bin/env python3
"""Verify an offline WebP candidate against saved PNGs, then time full rendering.

This never registers a URL format, serves an image, or publishes a Nostr event.
Use a benchmark generated from the same inputs and the approved preview gallery.
"""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
import weakref

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_images import distribution, environment, workloads

VARIANTS = ('png', 'png-webp', 'direct-webp')
TIMING_CASES = ('weather', 'eew', 'nankai-advisory', 'weather-cancel-reference')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def pixels(data):
    from io import BytesIO
    from PIL import Image
    with Image.open(BytesIO(data)) as image:
        image.load()
        with image.convert('RGBA') as rgba:
            return image.format, rgba.size, rgba.tobytes()


def rss():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {'peak_rss_mib': peak / (2**20 if sys.platform == 'darwin' else 1024)}
    statm = Path('/proc/self/statm')
    if statm.exists():
        result['current_rss_mib'] = int(statm.read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE') / 2**20
    return result


def worker(plan_path, output, *, variant=None, identifier=None, repeats=3):
    from unittest.mock import patch
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from scripts.webp_encoding_probe import render, transcode_png

    plan = json.loads(plan_path.read_text())
    output.mkdir(parents=True, exist_ok=False)
    renderer_refs = set()
    get_renderer = FigureCanvasAgg.get_renderer

    def track(canvas):
        renderer = get_renderer(canvas)
        renderer_refs.add(weakref.ref(renderer))
        return renderer

    result = {'environment': environment(), 'results': []}
    items = [item for item in plan['inputs'] if identifier is None or item['id'] == identifier]
    with patch.object(FigureCanvasAgg, 'get_renderer', track):
        for item in items:
            name = item['id']
            png = (Path(plan['baseline']) / 'images' / name / 'png-original.png').read_bytes()
            if digest(png) != item['png_sha256']:
                raise ValueError('Baseline PNG changed: ' + name)
            _, size, rgba = pixels(png)
            samples = []
            hashes = []
            for number in range(1 + repeats if variant else 1):
                started = time.perf_counter()
                image = render(item['records'], variant=variant or 'direct-webp',
                               cancel_reference=item['cancel_reference'] or None)
                elapsed = (time.perf_counter() - started) * 1000
                live = sum(reference() is not None for reference in renderer_refs)
                if live:
                    raise AssertionError('Agg retained after rendering: ' + name)
                actual_format, actual_size, actual_rgba = pixels(image.data)
                if (actual_size, actual_rgba) != (size, rgba):
                    raise AssertionError('RGBA pixels changed: ' + name)
                if image.decision.as_dict() != item['decision']:
                    raise AssertionError('Display decision changed: ' + name)
                expected_format = 'PNG' if variant == 'png' else 'WEBP'
                if actual_format != expected_format:
                    raise AssertionError('Wrong image format: ' + name)
                if variant == 'png' and image.data != png:
                    raise AssertionError('Original PNG bytes changed: ' + name)
                hashes.append(digest(image.data))
                samples.append({'render_ms': elapsed, 'stages': image.stages,
                                'live_renderers': live, **rss()})
            if len(set(hashes)) != 1:
                raise AssertionError('Encoding bytes vary between repeated renders: ' + name)
            extension = 'png' if variant == 'png' else 'webp'
            (output / (name + '.' + extension)).write_bytes(image.data)
            row = {'id': name, 'variant': variant or 'direct-webp', 'pixel_exact': True,
                   'decision_exact': True, 'bytes': len(image.data), 'png_bytes': len(png),
                   'sha256': hashes[0], 'samples': samples, 'usages': item['usages']}
            if variant:
                row.update(cold_render_ms=samples[0]['render_ms'],
                           warm_render_ms=distribution([s['render_ms'] for s in samples[1:]]))
            else:
                converted, stages = transcode_png(png)
                fmt, converted_size, converted_rgba = pixels(converted)
                if fmt != 'WEBP' or (converted_size, converted_rgba) != (size, rgba):
                    raise AssertionError('PNG transcode pixels changed: ' + name)
                (output / (name + '-transcoded.webp')).write_bytes(converted)
                row.update(transcoded_bytes=len(converted), transcode_stages=stages,
                           transcoded_sha256=digest(converted), same_webp_bytes=converted == image.data)
            result['results'].append(row)
            result['memory'] = rss()
            write(output / 'result.json', result)
            print(json.dumps({'id': name, 'variant': row['variant'], 'verified': len(result['results']),
                              'bytes': row['bytes'], 'pixel_exact': True}), flush=True)
    return result


def generate(output, *, baseline, gallery, selected_cases=None, timing_cases=TIMING_CASES,
             repeats=3, timeout=900):
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError('repeats must be an integer from 1 to 10')
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError('timeout must be an integer from 1 to 3600')
    if output.exists() or output.is_symlink():
        raise FileExistsError('Existing output is never overwritten: ' + str(output))
    items = workloads(selected_cases)
    cases = {case['slug']: case for case in json.loads((gallery / 'manifest.json').read_text())['cases']}
    for item in items:
        saved = json.loads((baseline / 'records' / (item['id'] + '.json')).read_text())
        for field in ('records', 'cancel_reference'):
            if tuple(tuple(row) for row in saved[field]) != item[field]:
                raise ValueError('Benchmark inputs changed: ' + item['id'])
        png = (baseline / 'images' / item['id'] / 'png-original.png').read_bytes()
        item['png_sha256'] = digest(png)
        for usage in item['usages']:
            case = cases[usage['case_slug']]
            source = case if usage['role'] == 'case' else case['posts'][usage['post_number'] - 1]
            image = source['images']['preview']
            if image['sha256'] != item['png_sha256']:
                raise ValueError('PNG does not match approved gallery: ' + item['id'])
            if 'decision' in item and item['decision'] != image['decision']:
                raise ValueError('Inconsistent gallery decision: ' + item['id'])
            item['decision'] = image['decision']
    known = {item['id'] for item in items}
    if any(name not in known for name in timing_cases):
        raise ValueError('Timing cases must be present in selected workloads')
    output.mkdir(parents=True)
    plan = {'baseline': str(baseline), 'inputs': items,
            'baseline_manifest_sha256': digest((baseline / 'manifest.json').read_bytes()),
            'gallery_manifest_sha256': digest((gallery / 'manifest.json').read_bytes())}
    write(output / 'plan.json', plan)
    report = {'kind': 'offline-webp-adoption-probe', 'profile': {
        'lossless': True, 'quality': 100, 'method': 4, 'exact': True},
        'settings': {'warm_repeats': repeats, 'timing_scope': 'full render including encoding and cleanup',
                     'excluded': 'pixel/hash comparisons, RSS inspection',
                     'import_scope': 'helper and Matplotlib preloaded; first timed call includes lazy preview import',
                     'workers': 'fresh process and font cache; serial; rotated variant order',
                     'corpus': 'one process, direct WebP plus transcode of saved approved PNGs',
                     'forced_gc': False}, 'timings': [], 'error': None}
    try:
        for index, name in enumerate(timing_cases):
            order = VARIANTS[index % 3:] + VARIANTS[:index % 3]
            for variant in order:
                target = output / 'timings' / (name + '-' + variant)
                command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
                           '--plan', str(output / 'plan.json'), '--output', str(target),
                           '--variant', variant, '--identifier', name, '--repeats', str(repeats)]
                print('Measuring ' + name + ': ' + variant, flush=True)
                with tempfile.TemporaryDirectory(prefix='webp-fonts-') as fonts:
                    subprocess.run(command, check=True, timeout=timeout,
                                   env=dict(os.environ, MPLCONFIGDIR=fonts))
                result = json.loads((target / 'result.json').read_text())
                report['environment'] = result['environment']
                report['timings'].append(result)
                write(output / 'manifest.json', report)
        with tempfile.TemporaryDirectory(prefix='webp-corpus-fonts-') as fonts:
            subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
                            '--plan', str(output / 'plan.json'), '--output', str(output / 'corpus')],
                           check=True, timeout=timeout, env=dict(os.environ, MPLCONFIGDIR=fonts))
        report['corpus'] = json.loads((output / 'corpus/result.json').read_text())
        report['verified_inputs'] = len(report['corpus']['results'])
        report['verified_gallery_usages'] = sum(len(row['usages']) for row in report['corpus']['results'])
        with (output / 'results.csv').open('x', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=('id', 'png_bytes', 'bytes', 'transcoded_bytes',
                                                       'pixel_exact', 'same_webp_bytes', 'sha256'))
            writer.writeheader()
            for row in report['corpus']['results']:
                writer.writerow({key: row[key] for key in writer.fieldnames})
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)[:500]
        raise
    finally:
        write(output / 'manifest.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--baseline', type=Path, default=ROOT / 'dist/benchmark')
    parser.add_argument('--gallery', type=Path, default=ROOT / 'dist/preview')
    parser.add_argument('--case', action='append')
    parser.add_argument('--timing-case', action='append')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--plan', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--identifier', help=argparse.SUPPRESS)
    parser.add_argument('--variant', choices=VARIANTS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.plan, args.output, variant=args.variant, identifier=args.identifier, repeats=args.repeats)
    else:
        generate(args.output.absolute(), baseline=args.baseline.absolute(), gallery=args.gallery.absolute(),
                 selected_cases=args.case, timing_cases=args.timing_case or TIMING_CASES,
                 repeats=args.repeats, timeout=args.timeout)


if __name__ == '__main__':
    main()
