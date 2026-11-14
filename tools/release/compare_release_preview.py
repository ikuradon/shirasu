#!/usr/bin/env python3
"""Match actual release HTTP PNGs to the approved gallery's exact post inputs."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.preview.gallery_compare import _Reader, _parse, _png, load_baseline
from tools.release.check_release_images import posting_content_matches, posting_tags


def require(value, message):
    if not value:
        raise ValueError('Preview comparison: ' + message)


def read_report(path):
    path = Path(path).absolute()
    reader = _Reader(path.parent)
    data = reader.read(path.name, 'manifest')
    value = _parse(data)
    require(isinstance(value, dict) and type(value.get('schema_version')) is int
            and value['schema_version'] == 1, 'invalid report schema')
    return value, hashlib.sha256(data).hexdigest(), reader


def keyed(rows):
    require(isinstance(rows, list) and rows, 'post list is empty or invalid')
    result = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get('case'), str)
                and type(row.get('number')) is int and row['number'] > 0, 'invalid post identity')
        key = row['case'], row['number']
        require(key not in result, 'duplicate post identity')
        result[key] = row
    return result


def same(first, second):
    # JSON comparison distinguishes false from 0 while normalizing tuple/list.
    return json.dumps(first, sort_keys=True, allow_nan=False) == json.dumps(second, sort_keys=True, allow_nan=False)


def check_png(reader, path, metadata, expected=None):
    png = reader.read(path, 'image')
    digest = hashlib.sha256(png).hexdigest()
    dimensions = _png(png)
    require(metadata.get('sha256') == digest and metadata.get('bytes') == len(png)
            and metadata.get('etag') == '"' + digest + '"'
            and dimensions == (metadata.get('width'), metadata.get('height')), 'HTTP PNG differs from its report')
    if expected is not None:
        require(digest == expected['sha256'] and dimensions == (expected['width'], expected['height']),
                'HTTP PNG differs from the approved post preview')


def compare(events, images, gallery, output):
    output, gallery = Path(output).absolute(), Path(gallery).absolute()
    image_root = Path(images).absolute().parent / (Path(images).stem + '-images')
    require(not output.exists() and not output.is_symlink(), 'output already exists')
    require(not output.resolve().is_relative_to(gallery.resolve()), 'output must be outside the approved gallery')
    require(not output.resolve().is_relative_to(image_root.resolve()), 'output must be outside the HTTP PNG directory')
    exported, events_sha, _ = read_report(events)
    report, images_sha, reader = read_report(images)
    bundle_sha = exported.get('bundle_manifest_sha256')
    require(isinstance(bundle_sha, str) and re.fullmatch('[0-9a-f]{64}', bundle_sha), 'invalid bundle fingerprint')
    require(exported.get('candidate_version') == report.get('candidate_version') == 'v5'
            and report.get('mode') == 'candidate'
            and report.get('bundle_manifest_sha256') == bundle_sha
            and report.get('receiver_export_sha256') == events_sha, 'HTTP result belongs to another export or candidate')
    baseline = load_baseline(gallery)
    gallery_raw = _Reader(gallery).read('manifest.json', 'manifest')
    cases = {case['slug']: case for case in baseline.manifest['cases']}
    inputs = {slug: records for slug, (_, records, _) in baseline._inputs}
    approved = {(slug, number): post for slug, case in cases.items()
                for number, post in enumerate(case.get('posts', ()), 1)}
    posts, http_posts = keyed(exported.get('posts')), keyed(report.get('posts'))
    require(set(posts) == set(http_posts) == set(approved), 'case/post set differs from the approved gallery')
    require({key[0] for key in posts} == set(cases), 'gallery case has no comparable posts')
    for value in (exported, report):
        require(type(value.get('case_count')) is int and value['case_count'] == len(cases)
                and type(value.get('post_count')) is int and value['post_count'] == len(posts), 'case/post counts differ')
    folder = Path(images).stem + '-images'
    expected_files, rendered, matched = set(), {}, 0
    for key, reference in approved.items():
        row, http = posts[key], http_posts[key]
        image_indexes = reference.get('image_record_indexes', reference['record_indexes'] if reference['image_enabled'] else [])
        for field, expected in (('kind', reference['kind']), ('text', reference['text']),
                                ('records', inputs[key[0]]), ('record_indexes', reference['record_indexes']),
                                ('image_record_indexes', image_indexes)):
            require(field in row and same(row[field], expected), f'{key}: {field} differs from the approved post')
        event = row.get('event', {})
        if not reference['image_enabled']:
            require(row.get('url') is None and row.get('alt') is None and http.get('image') is None
                    and event.get('content') == reference['text']
                    and event.get('tags') == posting_tags(reference['text']), 'text-only post changed')
            continue
        expected = reference['images'].get('preview')
        require(isinstance(expected, dict) and 'sha256' in expected, 'approved post has no valid preview PNG')
        url, alt = row.get('url'), row.get('alt')
        require(isinstance(url, str) and url.startswith('https://images.example.test/v5/rehearsal/')
                and http.get('url') == url and http.get('event_id') == event.get('id')
                and alt == http.get('alt') == expected.get('alt') and isinstance(alt, str), 'image URL, event or ALT differs')
        require(posting_content_matches(event.get('content'), url, reference['text'])
                and event.get('tags') == posting_tags(event['content'], url=url, alt=alt), 'signed post differs')
        metadata = http.get('image')
        require(isinstance(metadata, dict) and isinstance(metadata.get('path'), str)
                and metadata['path'].startswith(folder + '/'), 'invalid HTTP image path')
        check_png(reader, metadata['path'], metadata, expected)
        expected_files.add(metadata['path'])
        rendered[url] = metadata
        matched += 1
    require(report.get('attachment_count') == matched, 'HTTP attachment count differs')
    # Additional saved files are only the declared transition URLs not already
    # rendered for a catalog post. Names follow check_release_images output.
    require(isinstance(report.get('transition_posts'), list), 'missing HTTP transition records')
    for number, row in enumerate(report['transition_posts'], 1):
        require(isinstance(row, dict), 'invalid HTTP transition record')
        url = row.get('url')
        if url is None:
            require(row.get('image') is None, 'invalid text-only transition')
            continue
        require(isinstance(url, str), 'invalid transition URL')
        version = urlsplit(url).path.split('/')[1]
        require(version in ('v1', 'v2', 'v3', 'v4', 'v5'), 'invalid transition generation')
        if url not in rendered:
            path = f'{folder}/transition-{number:02d}-{version}.png'
            check_png(reader, path, row)
            expected_files.add(path)
            rendered[url] = row
        require(all(row.get(name) == rendered[url].get(name) for name in ('sha256', 'bytes', 'width', 'height', 'etag')),
                'transition PNG metadata changed')
    actual = set()
    for path in image_root.rglob('*'):
        require(not path.is_symlink() and path.is_file(), 'unexpected HTTP image directory or symbolic link')
        actual.add(path.relative_to(reader.root).as_posix())
    require(actual == expected_files, 'saved HTTP PNG files are missing or undeclared')
    result = {'schema_version': 1, 'bundle_manifest_sha256': bundle_sha,
              'receiver_export_sha256': events_sha, 'http_report_sha256': images_sha,
              'gallery_manifest_sha256': hashlib.sha256(gallery_raw).hexdigest(),
              'matched_cases': len(cases), 'matched_posts': len(posts), 'matched_pngs': matched,
              'verified_saved_pngs': len(expected_files),
              'method': 'Exact post inputs, text, ALT and byte-identical PNG SHA-256; commit labels are not compared.'}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as destination:
        json.dump(result, destination, ensure_ascii=False, indent=2)
        destination.write('\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('events', 'images', 'gallery', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = compare(args.events, args.images, args.gallery, args.output)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
