#!/usr/bin/env python3
"""Export only public verification results; never upload a renderer package."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from tools.preview.gallery_compare import load_baseline
from tools.preview.gallery_html import render_html
from tools.release.prepare_release_candidate import verify_bundle


REPORTS = ('posts.json', 'legacy.json', 'images.json', 'webp-posts.json',
           'webp-http.json', 'compose.json')
MANIFEST = 'public-manifest.json'


def require(condition, message):
    if not condition:
        raise ValueError('Public CI artifact: ' + message)


def relative(name):
    require(isinstance(name, str) and len(name) <= 512
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', name) is not None
            and not PurePosixPath(name).is_absolute()
            and all(part not in ('', '.', '..') for part in name.split('/')),
            'unsafe file path')
    return name


def read(root, name, maximum=32 * 1024 * 1024):
    path = Path(root)
    require(path.is_dir() and not path.is_symlink(), 'expected an unlinked directory')
    for part in relative(name).split('/'):
        path /= part
        require(not path.is_symlink(), 'linked input is not allowed')
    require(path.is_file(), 'missing regular file: ' + name)
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    require(len(data) <= maximum, 'file exceeds the artifact limit')
    return data


def parse(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'duplicate JSON key')
            value[key] = item
        return value
    result = json.loads(data, object_pairs_hook=unique,
                        parse_constant=lambda _: require(False, 'non-finite JSON'))
    require(isinstance(result, dict), 'expected a JSON object')
    return result


def fingerprint(data):
    return {'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}


def true_checks(report, names):
    checks = report.get('checks', {})
    require(isinstance(checks, dict) and all(checks.get(name) is True for name in names),
            'required verification did not pass')


def collect(root, candidate_raw, *, gallery_root, render_gallery=False):
    """Read a fixed allowlist and typed gallery assets into a byte snapshot."""
    root = Path(root)
    candidate = parse(candidate_raw)
    candidate_sha = fingerprint(candidate_raw)['sha256']
    require(candidate.get('candidate_version') == 'v5', 'unexpected candidate version')
    provenance = candidate.get('source')
    require(isinstance(provenance, dict) and set(provenance) == {'commit', 'working_tree'}
            and ((provenance['commit'] is None and provenance['working_tree'] is None)
                 or (isinstance(provenance['commit'], str)
                     and re.fullmatch('[0-9a-f]{40}', provenance['commit']) is not None
                     and type(provenance['working_tree']) is bool)), 'invalid source provenance')
    files = {'candidate-manifest.json': candidate_raw}
    reports = {}
    for name in REPORTS:
        data = read(root, 'results/' + name)
        files['results/' + name] = data
        reports[name] = parse(data)
    comparison = read(root, 'source-comparison.json')
    source = parse(comparison)
    require(len(source.get('unchanged_files', [])) + len(source.get('relocated_imports_verified', [])) == 42
            and set(source.get('relocated_imports_verified', [])) <= {'image_service/__main__.py'}
            and set(source.get('profile_changes_verified', [])) <= {'image_versions.py', 'image_protocol.py', 'image_service/app.py'}
            and set(source.get('routing_changes_require_runtime_checks', [])) <= {'image_versions.py', 'image_protocol.py', 'image_service/app.py'},
            'published source comparison is incomplete')
    files['source-comparison.json'] = comparison
    for name in REPORTS:
        report = reports[name]
        require(type(report.get('schema_version')) is int and report['schema_version'] == 1,
                'unknown verification report')
        if name != 'legacy.json':
            require(report.get('bundle_manifest_sha256') == candidate_sha,
                    'verification reports refer to different candidates')
    for name in ('posts.json', 'webp-posts.json', 'images.json', 'webp-http.json'):
        report = reports[name]
        require(report.get('candidate_version') == 'v5' and report.get('case_count') == 40
                and report.get('post_count') == len(report.get('posts', [])) == 46
                and len(report.get('transition_posts', [])) == 20, 'post verification is incomplete')
    for name in ('posts.json', 'webp-posts.json'):
        true_checks(reports[name], ('runtime_from_bundle', 'signed_events_verified',
                                   'catalog_complete_text_nmea_url_alt', 'restart_retry_event_unchanged'))
        require(reports[name]['checks'].get('network_connections') == 0,
                'receiver verification used external connections')
    require(reports['webp-posts.json'].get('mode') == 'webp-receiver'
            and reports['webp-posts.json'].get('encoding') == 'w1', 'missing WebP event verification')
    true_checks(reports['webp-posts.json'], ('real_nostr_mime',))
    png_hash = fingerprint(files['results/posts.json'])['sha256']
    for name in ('legacy.json', 'images.json'):
        require(reports[name].get('receiver_export_sha256') == png_hash,
                'PNG HTTP verification refers to different events')
    legacy, png, webp, compose = (reports[name] for name in
                                ('legacy.json', 'images.json', 'webp-http.json', 'compose.json'))
    require(legacy.get('mode') == 'legacy' and len(legacy.get('records', [])) == 5
            and png.get('mode') == 'candidate'
            and png.get('legacy', {}).get('baseline_sha256') == fingerprint(files['results/legacy.json'])['sha256']
            and len(png['legacy'].get('records', [])) == 5, 'old URL comparison is incomplete')
    true_checks(legacy, ('isolated_runtime', 'real_wsgi_pngs'))
    true_checks(png, ('isolated_runtime', 'health', 'real_wsgi_pngs', 'event_ids', 'signed_inputs',
                      'imeta_and_body', 'head', 'conditional_get', 'artifact_unchanged'))
    require(webp.get('mode') == 'webp-http' and webp.get('encoding') == 'w1'
            and webp.get('receiver_export_sha256') == fingerprint(files['results/webp-posts.json'])['sha256']
            and webp.get('verified_http_inputs') == len(webp.get('corpus', [])) == 45
            and webp.get('approved_unique_inputs') == 49 and len(webp.get('offline_only_inputs', [])) == 4,
            'WebP HTTP verification is incomplete or uses different events')
    true_checks(webp, ('real_loopback_http', 'fixed_candidate', 'approved_post_inputs_text_alt',
                       'imeta_and_body', 'health', 'rgba_pixels_exact', 'head',
                       'conditional_get', 'accept_does_not_negotiate'))
    require(compose.get('mode') == 'release-compose' and len(compose.get('stages', [])) == 3
            and compose.get('checks') and all(value is True for value in compose['checks'].values()),
            'Compose verification is incomplete')

    def image(row, field='path'):
        if row is None:
            return
        require(isinstance(row, dict) and field in row, 'missing HTTP image path')
        name = relative(row[field])
        require(PurePosixPath(name).suffix in ('.png', '.webp'), 'only verified images may be exported')
        data = read(root / 'results', name)
        require(fingerprint(data)['sha256'] == row.get('sha256'), 'HTTP image checksum differs')
        require((name.endswith('.png') and data.startswith(b'\x89PNG\r\n\x1a\n'))
                or (name.endswith('.webp') and data[:4] == b'RIFF' and data[8:12] == b'WEBP'),
                'HTTP image format differs')
        files['results/' + name] = data

    for row in legacy['records']:
        image(row, 'png')
    for report in (png, webp):
        for row in report['posts'] + report['transition_posts']:
            image(row.get('image'))
    for row in webp['corpus']:
        require(row.get('pixel_exact') is True, 'WebP pixels did not match')
        image(row)

    gallery_root = Path(gallery_root)
    gallery_raw = read(gallery_root, 'manifest.json')
    require(fingerprint(gallery_raw)['sha256'] == webp.get('gallery_manifest_sha256'),
            'gallery differs from the HTTP verification')
    baseline = load_baseline(gallery_root)
    require(baseline.manifest.get('error_count') == 0 and len(baseline.manifest['cases']) == 40,
            'gallery is incomplete or contains render errors')
    files['png-gallery/manifest.json'] = gallery_raw
    # Regenerate HTML from validated records rather than copying arbitrary files.
    # The gallery loader drops comparison-only assets that were not verified here.
    # Later verification uses the saved HTML checksum, so future template changes
    # do not invalidate an otherwise intact historical artifact.
    files['png-gallery/index.html'] = (render_html(baseline.manifest).encode('utf-8')
                                       if render_gallery else read(gallery_root, 'index.html'))
    for name, role, data in baseline._assets:
        require(role in ('image', 'records', 'license'), 'unknown gallery asset role')
        files['png-gallery/' + relative(name)] = data
    return candidate, files


def metadata(candidate, files):
    return {'schema_version': 1,
            'candidate_manifest_sha256': fingerprint(files['candidate-manifest.json'])['sha256'],
            'source_commit': candidate['source']['commit'],
            'source_dirty': candidate['source']['working_tree'],
            'candidate_version': candidate['candidate_version'],
            'files': {name: fingerprint(data) for name, data in sorted(files.items())}}


def verify_public(directory):
    """Check a downloaded public artifact without Docker, Git or private GIS."""
    root = Path(directory).absolute()
    declared = parse(read(root, MANIFEST))
    candidate, expected = collect(root, read(root, 'candidate-manifest.json'),
                                  gallery_root=root / 'png-gallery')
    actual = set()
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'linked artifact entry is not allowed')
        if path.is_dir():
            require(any(name.startswith(path.relative_to(root).as_posix() + '/') for name in expected),
                    'unexpected artifact directory')
            continue
        require(path.is_file(), 'unexpected artifact entry')
        actual.add(path.relative_to(root).as_posix())
    require(actual == set(expected) | {MANIFEST}, 'unexpected or missing artifact files')
    require(all(read(root, name) == data for name, data in expected.items()), 'artifact bytes differ')
    require(declared == metadata(candidate, expected), 'artifact checksums or provenance differ')
    return declared


def prepare(check_root, output):
    root, output = Path(check_root).absolute(), Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'output already exists')
    require(not any(parent.is_symlink() for parent in output.parents),
            'linked output directory is not allowed')
    require(not output.resolve().is_relative_to((root / 'candidate').resolve()),
            'output must be outside the fixed candidate')
    before = verify_bundle(root / 'candidate')
    raw = read(root / 'candidate', 'candidate-manifest.json')
    require(fingerprint(raw)['sha256'] == before['manifest_sha256'], 'candidate changed while reading')
    candidate, files = collect(root, raw, gallery_root=root / 'results/png-gallery', render_gallery=True)
    require(verify_bundle(root / 'candidate') == before, 'candidate changed during artifact preparation')
    result = metadata(candidate, files)
    output.mkdir(parents=True)
    try:
        for name, data in files.items():
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (output / MANIFEST).write_text(json.dumps(result, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        verify_public(output)
    except Exception:
        shutil.rmtree(output)
        raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.check_root, args.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('Public CI artifact failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps({'candidate_manifest_sha256': result['candidate_manifest_sha256'],
                      'files': len(result['files']), 'output': str(args.output)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
