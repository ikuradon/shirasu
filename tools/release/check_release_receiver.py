#!/usr/bin/env python3
"""Export verified, locally signed posts from an isolated release candidate.

Run with the candidate receiver directory first on PYTHONPATH. Only source
fixtures and the offline planning helpers come from the checkout. No service
settings, deployment key, existing SQLite database or transport is used.
"""

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit


from tools.release.layout import MODULES
from tools.release.check_release_images import posting_tags


KEY = bytes(range(32))  # Public rehearsal key, never a deployment credential.
KEY_ID = 'rehearsal'
ORIGIN = 'https://images.example.test'
RUNTIME_MODULES = ('main', 'image_publication', 'image_protocol', 'image_versions',
                   'image_alt', 'nankai_image', 'message_format', 'nankai',
                   'outbox', 'typhoon', 'receiver', 'dcx_instructions')


def require(condition, message):
    if not condition:
        raise ValueError('Receiver rehearsal: ' + message)


def runtime_modules(bundle):
    """Reject a source checkout accidentally shadowing any receiver module."""
    directory = (Path(bundle) / 'receiver').resolve()
    legacy = (directory / 'main.py').is_file()
    modules = {}
    for name in RUNTIME_MODULES:
        import_name = name if legacy else MODULES[name]
        module = importlib.import_module(import_name)
        location = Path(getattr(module, '__file__', '')).resolve()
        require(location == directory / (import_name.replace('.', '/') + '.py'), 'module is outside the candidate: ' + name)
        modules[name] = module
    # Retained candidates predate the shared footer module. New candidates
    # must inspect footer tags from the same immutable receiver artifact.
    publication = directory / 'shirasu/reports/publication.py'
    if publication.is_file():
        module = importlib.import_module('shirasu.reports.publication')
        require(Path(module.__file__).resolve() == publication,
                'module is outside the candidate: report_publication')
        modules['report_publication'] = module
    return modules


def forbidden(*args, **kwargs):
    raise RuntimeError('Receiver rehearsal must not load configuration or use a network')


@contextmanager
def isolated_runtime(modules):
    import builtins
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == 'config':
            forbidden()
        return original_import(name, *args, **kwargs)

    with patch.object(modules['main'].Settings, 'load', forbidden), \
            patch.object(modules['main'].logger, 'disabled', True), \
            patch.object(builtins, '__import__', guarded_import), \
            patch.object(socket, 'create_connection', forbidden), \
            patch.object(socket, 'getaddrinfo', forbidden), \
            patch.object(socket.socket, 'connect', forbidden), \
            patch.object(socket.socket, 'connect_ex', forbidden):
        yield


class Clock:
    def __init__(self):
        self.now = 0.

    def __call__(self):
        return self.now


class LocalTransport:
    """Real SDK event signing with only the delivery boundary replaced."""
    def __init__(self, main):
        self.main = main
        self.keys = main.Keys.generate()
        self.identity = self.keys.public_key().to_hex()
        self.attempts, self.sent = [], []
        self.fail = False

    def build_event(self, message, image_url=None, image_alt=None):
        return self.main.Nostr.build_event(self, message, image_url=image_url, image_alt=image_alt)

    async def send(self, event):
        self.attempts.append(event)
        if self.fail:
            raise self.main.DeliveryError('rehearsal delivery failure')
        self.sent.append(event)

    async def connect(self):
        forbidden()

    async def connected(self):
        forbidden()


def publisher(modules, version):
    return (None if version is None else
            modules['image_publication'].ImagePublisher(ORIGIN, KEY_ID, KEY, version=version))


def decode(entry):
    import azarashi
    return azarashi.decode(entry[0], timestamp=datetime.fromtimestamp(entry[1] / 1000, timezone.utc))


async def drain(service):
    for _ in range(128):
        if not await service.publish_one():
            return
    raise ValueError('Receiver rehearsal did not become idle')


def event_details(modules, event, text, entries, images):
    require(event.verify() and event.kind().as_u16() == 1, 'invalid signed text event')
    tags = [tag.to_vec() for tag in event.tags()]
    url, alt = None, None
    if entries:
        require(images is not None, 'an image requires a configured publisher')
        url = images.url(entries)
        alt = modules['image_alt'].image_alt_text(entries)
        require(tags == posting_tags(event.content(), url=url, alt=alt),
                'image or hashtag metadata differs from the signed post')
        parsed = urlsplit(url)
        require(parsed.scheme == 'https' and parsed.netloc == 'images.example.test'
                and not parsed.query and not parsed.fragment, 'unexpected attachment origin or query')
        actual = modules['image_protocol'].parse_path(parsed.path, {KEY_ID: KEY})
        require(actual == tuple(sorted(entries, key=lambda entry: (entry[1], entry[0]))),
                'signed URL lost the original image inputs')
        expected_text = modules['image_publication'].with_image(text, url)
    else:
        require(tags == posting_tags(event.content()), 'a text-only event has unexpected metadata')
        expected_text = text
    require(event.content() == expected_text, 'signed content differs from the complete planned post')
    return {'event': json.loads(event.as_json()), 'url': url, 'alt': alt}


async def replay_case(modules, case, version):
    from tools.preview.gallery_posts import _isolated_decoder, plan_posts
    images = publisher(modules, version)
    topics = images is not None and images.supports_nankai_topic
    planned = plan_posts(case.records, nankai_topics=topics)
    clock, transport = Clock(), LocalTransport(modules['main'])
    with _isolated_decoder(), modules['outbox'].Outbox(':memory:', transport.identity) as box:
        service = modules['main'].Service(transport, box, asyncio.Event(), clock=clock, images=images)
        for entry in case.records:
            result = await service.accept(decode(entry))
            require(result in ('queued', 'buffered', 'duplicate', 'ignored'), 'input was not accepted')
            await drain(service)
        clock.now = 180.
        await drain(service)
        require(len(transport.sent) == len(planned), 'collector changed the number of posts')
        require(box.snapshot(clock())['counts']['pending'] == 0, 'catalog replay left a pending post')
        result = []
        for number, (post, event) in enumerate(zip(planned, transport.sent, strict=True), 1):
            require(event.author().to_hex() == transport.identity, 'event author changed')
            records = tuple(case.records[index] for index in post.record_indexes)
            entries = tuple(case.records[index] for index in post.image_record_indexes)
            require(bool(entries) == post.image_enabled, 'planned image input is inconsistent')
            require(post.text.partition('\n---\n')[2].splitlines() ==
                    ['NMEA: ' + nmea for nmea, _ in records], 'post lost an original NMEA page')
            result.append({'case': case.slug, 'number': number, 'kind': post.kind,
                           'text': post.text, 'records': case.records,
                           'record_indexes': post.record_indexes,
                           'image_record_indexes': post.image_record_indexes,
                           **event_details(modules, event, post.text, entries, images)})
        return result


def transition(modules, label, event, text, entries, images):
    return {'label': label, 'entries': entries,
            **event_details(modules, event, text, entries, images)}


async def receive_edges(modules, cases, candidate, rollback):
    """Check complete/cancel capability and missing -> late-complete delivery."""
    from tools.preview.gallery_posts import _isolated_decoder, plan_posts
    transitions = []
    for slug in ('nankai-advisory', 'nankai-ended', 'nankai-cancel'):
        post, = await replay_case(modules, cases[slug], rollback)
        require(post['url'] is None, 'legacy version enabled a Nankai topic')
        transitions.append({'label': 'legacy-' + slug, 'entries': (),
                            'event': post['event'], 'url': None, 'alt': None})
    advisory, cancelled = cases['nankai-advisory'], cases['nankai-cancel']
    for version in (rollback, candidate, None):
        label = version or 'images-off'
        images = publisher(modules, version)
        topics = images is not None and images.supports_nankai_topic
        clock, transport = Clock(), LocalTransport(modules['main'])
        with _isolated_decoder(), modules['outbox'].Outbox(':memory:', transport.identity) as box:
            service = modules['main'].Service(transport, box, asyncio.Event(), clock=clock, images=images)
            require(await service.accept(decode(advisory.records[0])) == 'buffered', 'first page not buffered')
            require(not await service.publish_one(), 'incomplete text was posted before its deadline')
            clock.now = 180.
            require(await service.publish_one(), 'missing-text notice was not posted')
            missing, = plan_posts(advisory.records[:1], nankai_topics=topics)
            require('【本文未受信】' in missing.text, 'expected missing-text marker absent')
            notice = transport.sent[-1]
            transitions.append(transition(modules, label + '-missing', notice, missing.text, (), images))
            saved = notice.as_json()
            clock.now = 181.
            for entry in advisory.records[1:]:
                require(await service.accept(decode(entry)) == 'buffered', 'late page not buffered')
            require(await service.publish_one(), 'late completion was not posted')
            complete, = plan_posts(advisory.records, nankai_topics=topics)
            entries = tuple(advisory.records[index] for index in complete.image_record_indexes)
            transitions.append(transition(modules, label + '-late-complete', transport.sent[-1],
                                          '【追加更新】' + complete.text, entries, images))
            persisted = box.connection.execute('SELECT event_json FROM events WHERE event_id=?',
                                               (notice.id().to_hex(),)).fetchone()['event_json']
            require(persisted == saved, 'late completion rewrote the earlier notice')
            require(not await service.publish_one(), 'late completion was posted twice')
        clock, transport = Clock(), LocalTransport(modules['main'])
        with _isolated_decoder(), modules['outbox'].Outbox(':memory:', transport.identity) as box:
            service = modules['main'].Service(transport, box, asyncio.Event(), clock=clock, images=images)
            await service.accept(decode(advisory.records[0]))
            require(not await service.publish_one(), 'partial original was posted')
            for entry in cancelled.records:
                require(await service.accept(decode(entry)) == 'buffered', 'cancellation not buffered')
            require(await service.publish_one() and clock() == 0., 'cancellation was not immediate')
            post, = plan_posts(cancelled.records, nankai_topics=topics)
            entries = tuple(cancelled.records[index] for index in post.image_record_indexes)
            transitions.append(transition(modules, label + '-cancel', transport.sent[-1], post.text, entries, images))
            clock.now = 180.
            require(not await service.publish_one(), 'cancelled original was published later')
    return transitions


async def switch_and_retry(modules, cases, candidate, rollback):
    """Rollback changes only new URLs; saved events retain their original URL."""
    from tools.preview.gallery_posts import _isolated_decoder, plan_posts
    clock, transport = Clock(), LocalTransport(modules['main'])
    transitions, saved = [], []
    stages = ((rollback, 'marine'), (candidate, 'nankai-advisory'),
              (rollback, 'hypocenter'), (None, 'weather'))
    with tempfile.TemporaryDirectory(prefix='shirasu-release-retry-') as directory, _isolated_decoder():
        path = Path(directory) / 'rehearsal.sqlite3'
        prior = None
        for number, (version, slug) in enumerate(stages, 1):
            images = publisher(modules, version)
            with modules['outbox'].Outbox(path, transport.identity) as box:
                service = modules['main'].Service(transport, box, asyncio.Event(), clock=clock, images=images)
                if prior is not None:
                    text, entries, old_images, event_json = prior
                    clock.now += 2
                    transport.fail = False
                    require(await service.publish_one(), 'pending event was not retried after restart')
                    event = transport.sent[-1]
                    require(event.as_json() == event_json, 'restart/version rollback rewrote a signed event')
                    transitions.append(transition(modules, f'retry-{number}', event, text, entries, old_images))
                case = cases[slug]
                topics = images is not None and images.supports_nankai_topic
                post, = plan_posts(case.records, nankai_topics=topics)
                entries = tuple(case.records[index] for index in post.image_record_indexes) if images else ()
                for entry in case.records:
                    require(await service.accept(decode(entry)) in ('queued', 'buffered'), 'new stage was not queued')
                transport.fail = True
                require(await service.publish_one(), 'simulated delivery failure did not attempt a post')
                event = transport.attempts[-1]
                require(box.snapshot(clock())['counts']['pending'] == 1, 'failed event was not retained')
                transitions.append(transition(modules, f'new-{number}-{version or "images-off"}',
                                              event, post.text, entries, images))
                prior = post.text, entries, images, event.as_json()
                saved.append((event.id().to_hex(), event.as_json()))
        with modules['outbox'].Outbox(path, transport.identity) as box:
            service = modules['main'].Service(transport, box, asyncio.Event(), clock=clock, images=None)
            clock.now += 2
            transport.fail = False
            require(await service.publish_one(), 'final text-only event was not retried')
            text, entries, old_images, event_json = prior
            require(transport.sent[-1].as_json() == event_json, 'image disable rewrote the saved event')
            transitions.append(transition(modules, 'retry-images-off', transport.sent[-1], text, entries, old_images))
            for event_id, original in saved:
                row = box.connection.execute('SELECT event_json FROM events WHERE event_id=?', (event_id,)).fetchone()
                require(row is not None and row['event_json'] == original, 'saved event changed during version switches')
            require(box.snapshot(clock())['counts'] == {'pending': 0, 'sent': 4, 'expired': 0},
                    'version switch replay lost or duplicated an event')
    return transitions


async def run_checks(modules, manifest):
    from tools.preview.gallery_catalog import load_cases
    candidate, rollback = manifest['candidate_version'], manifest['rollback_version']
    profile = modules['image_versions']
    require(profile.CANDIDATE_VERSION == candidate, 'candidate generation differs from the bundle')
    require(publisher(modules, candidate).supports_nankai_topic is True, 'candidate topic capability disabled')
    require(all(publisher(modules, version).supports_nankai_topic is False for version in ('v1', 'v2', 'v3', 'v4')),
            'a published generation enabled a Nankai topic')
    cases = {case.slug: case for case in load_cases()}
    require(len(cases) == 40, 'expected the complete 40-case catalog')
    posts = []
    for case in cases.values():
        posts.extend(await replay_case(modules, case, candidate))
    require(len(posts) == 46, 'expected 46 complete catalog posts')
    transitions = await receive_edges(modules, cases, candidate, rollback)
    transitions += await switch_and_retry(modules, cases, candidate, rollback)
    return {'schema_version': 1, 'bundle_manifest_sha256': manifest['manifest_sha256'],
            'candidate_version': candidate, 'rollback_version': rollback,
            'case_count': len(cases), 'post_count': len(posts), 'posts': posts,
            'transition_posts': transitions,
            'checks': {'runtime_from_bundle': True, 'signed_events_verified': True,
                       'catalog_complete_text_nmea_url_alt': True, 'candidate_nankai_images': 3,
                       'legacy_nankai_text_only': 3, 'missing_notice_text_only': True,
                       'late_completion_preserves_notice': True, 'cancellation_immediate': True,
                       'restart_retry_event_unchanged': True,
                       'version_sequence': [rollback, candidate, rollback, None],
                       'saved_event_count': 4, 'network_connections': 0}}


def check(bundle, output):
    from tools.release.prepare_release_candidate import verify_bundle
    bundle, output = Path(bundle).absolute(), Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'output already exists')
    require(not output.resolve().is_relative_to(bundle.resolve()), 'output must be outside the immutable bundle')
    manifest = verify_bundle(bundle)
    modules = runtime_modules(bundle)
    with isolated_runtime(modules):
        result = asyncio.run(run_checks(modules, manifest))
    require(verify_bundle(bundle) == manifest, 'candidate changed during the receiver checks')
    serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as destination:
        destination.write(serialized)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = check(args.bundle, args.output)
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        parser.exit(1, str(error) + '\n')
    print(f"Receiver checks passed: {result['case_count']} cases / {result['post_count']} posts / "
          f"{len(result['transition_posts'])} transition events: {args.output}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
