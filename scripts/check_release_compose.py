#!/usr/bin/env python3
"""Rehearse the fixed receiver image with an isolated, real Compose project.

Only synthetic configuration, public test keys and a new named volume are used.
The real receiver starts with networking disabled; failed health checks are
expected because no GPSD or Nostr relay is available. No live settings are read.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))


# Run this helper inside the frozen image, whose /app remains first on sys.path.
# It never connects a transport and never changes the application's entry point.
PROBE = r'''
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
import sys

sys.path.insert(0, '/app')
import azarashi
import config
import main
from nostr_sdk import Event
from outbox import Outbox, read_history, read_status

NMEA = {
 'webp': '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05',
 'png': '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B',
}
settings = main.Settings.load(config)
assert settings.state_path == '/data/shirasu.sqlite3'
assert settings.gpsd_host == '127.0.0.1' and settings.gpsd_port == 9
assert settings.relay_urls == 'ws://127.0.0.1:9'

def events():
 result = {}
 for row in read_history(settings.state_path, limit=20)['events']:
  event = Event.from_json(row['event_json'])
  assert event.verify() and event.id().to_hex() == row['event_id']
  raw = json.loads(row['event_json'])
  tags = [tag for tag in raw['tags'] if tag[0] == 'imeta']
  assert len(tags) == 1
  fields = {field.split(' ', 1)[0]: field.split(' ', 1)[1] for field in tags[0][1:]}
  assert fields['url'] in raw['content'] and fields['alt']
  result[row['event_id']] = {
   'event_id': row['event_id'], 'signature': raw['sig'],
   'event_json_sha256': hashlib.sha256(row['event_json'].encode()).hexdigest(),
   'url': fields['url'], 'mime': fields['m'],
  }
 return result

async def seed(name):
 client = main.Nostr(settings.secret_key, settings.relay_urls)
 try:
  with Outbox(settings.state_path, client.identity, settings.dedup_seconds,
              settings.retry_max_age, settings.max_pending) as box:
   service = main.Service(client, box, asyncio.Event(), images=settings.images,
                          typhoon_wait_seconds=settings.typhoon_wait_seconds,
                          nankai_wait_seconds=settings.nankai_wait_seconds)
   report = azarashi.decode(NMEA[name], timestamp=datetime(2026, 11, 8, tzinfo=timezone.utc))
   assert await service.accept(report) == 'queued'
 finally:
  await client.close()

mode = sys.argv[1]
if mode == 'seed':
 asyncio.run(seed(sys.argv[2]))
 result = events()
elif mode == 'events':
 result = events()
elif mode == 'status':
 result = read_status(settings.state_path)
elif mode == 'settings':
 result = {'version': settings.images.version, 'encoding': settings.images.encoding,
           'mime': settings.images.mime,
           'marker': os.environ.get('RELEASE_COMPOSE_REHEARSAL_MARKER')}
else:
 raise ValueError('Unknown probe operation')
print(json.dumps(result, sort_keys=True))
'''


def require(condition, message):
    if not condition:
        raise ValueError('Compose rehearsal: ' + message)


def command(arguments, *, environment, cwd, timeout=60, accepted=(0,)):
    result = subprocess.run(arguments, cwd=cwd, env=environment,
                            capture_output=True, text=True, timeout=timeout, check=False)
    require(result.returncode in accepted,
            f'command failed ({result.returncode}): {arguments[0]} '
            + result.stderr[-4000:].strip())
    return result


def check(bundle, image, output):
    from scripts.prepare_release_candidate import verify_bundle
    from scripts.prepare_release_compose import prepare

    bundle, output = Path(bundle).resolve(), Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'output already exists')
    require(not output.resolve().is_relative_to(bundle), 'output must be outside the candidate')
    output.parent.mkdir(parents=True, exist_ok=True)
    before = verify_bundle(bundle)
    root = Path(tempfile.mkdtemp(prefix='shirasu-compose-rehearsal-', dir=output.parent))
    project = 'shirasu-release-check-' + uuid.uuid4().hex[:12]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('COMPOSE_', 'SHIRASU_'))}
    paths = {name: root / name for name in
             ('compose.yaml', 'compose.host.yaml', 'compose.release.yaml',
              'compose.isolated.yaml', 'test.env', 'config.py', 'probe.py')}
    cleanup_required = False
    completed = False
    try:
        release = prepare(bundle, image, paths['compose.release.yaml'])
        image_id = release['image_id']
        require(release['bundle_manifest_sha256'] == before['manifest_sha256'],
                'prepared override refers to another candidate')
        shutil.copyfile(ROOT / 'compose.yaml', paths['compose.yaml'])
        paths['compose.host.yaml'].write_text('''services:
  shirasu:
    stop_grace_period: 17s
    environment:
      RELEASE_COMPOSE_REHEARSAL_MARKER: synthetic-host-override
''')
        paths['compose.isolated.yaml'].write_text('''services:
  shirasu:
    network_mode: none
    restart: "no"
    healthcheck:
      interval: 1s
      start_period: 0s
      retries: 1
    volumes:
      - type: bind
        source: ./probe.py
        target: /rehearsal/probe.py
        read_only: true
''')
        paths['config.py'].write_text('''# Public test key; no deployment credentials.
SECRET_KEY = "0000000000000000000000000000000000000000000000000000000000000001"
RELAY_URLS = "ws://127.0.0.1:9"
GPSD_HOST = "127.0.0.1"
GPSD_PORT = 9
GPSD_READ_TIMEOUT = 1
RETRY_MAX_AGE_SECONDS = 0
''')
        paths['probe.py'].write_text(PROBE)

        def settings(version, encoding):
            paths['test.env'].write_text(
                'SHIRASU_IMAGE_BASE_URL=https://images.example.test\n'
                'SHIRASU_IMAGE_KEY_ID=rehearsal\n'
                'SHIRASU_IMAGE_SIGNING_KEY=' + bytes(range(32)).hex() + '\n'
                f'SHIRASU_IMAGE_VERSION={version}\nSHIRASU_IMAGE_ENCODING={encoding}\n')
            paths['test.env'].chmod(0o600)

        def compose(*arguments, isolated=True, accepted=(0,), timeout=60):
            base = ['docker', 'compose', '--project-directory', str(root),
                    '--project-name', project, '--env-file', str(paths['test.env'])]
            files = ['compose.yaml', 'compose.host.yaml', 'compose.release.yaml']
            if isolated:
                files.append('compose.isolated.yaml')
            for name in files:
                base += ['-f', str(paths[name])]
            return command([*base, *arguments], environment=environment, cwd=root,
                           accepted=accepted, timeout=timeout)

        def probe(operation, *arguments, running=False):
            prefix = (['exec', '-T', 'shirasu'] if running else
                      ['run', '--rm', '--no-deps', '--pull', 'never', '-T', 'shirasu'])
            result = compose(*prefix, 'python', '-B', '/rehearsal/probe.py', operation, *arguments)
            return json.loads(result.stdout)

        def inspect():
            container = compose('ps', '--all', '--quiet', 'shirasu').stdout.strip()
            require(bool(container) and '\n' not in container, 'expected exactly one receiver container')
            result = command(['docker', 'inspect', container],
                             environment=environment, cwd=root)
            return json.loads(result.stdout)[0]

        settings('v5', 'w1')
        model = json.loads(compose('config', '--format', 'json', isolated=False).stdout)
        require(model['name'] == project, 'Compose project changed')
        for name in ('shirasu', 'metrics', 'backup'):
            service = model['services'][name]
            require(not service.get('build') and service['image'] == image_id
                    and service['pull_policy'] == 'never', 'service can use an unverified build: ' + name)
        receiver = model['services']['shirasu']
        config_bind = next(v for v in receiver['volumes'] if v['target'] == '/app/config.py')
        volume = next(v for v in receiver['volumes'] if v['target'] == '/data')
        volume_name = model['volumes'][volume['source']]['name']
        require(volume_name.startswith(project + '_'), 'state volume is outside the test project')
        require(config_bind['source'] == str(paths['config.py']) and config_bind['read_only'],
                'existing config bind was not retained')
        require(receiver['network_mode'] == 'host'
                and receiver['stop_grace_period'] == '17s'
                and receiver['healthcheck']['test'] == ['CMD', 'python', 'main.py', '--health']
                and receiver['environment']['RELEASE_COMPOSE_REHEARSAL_MARKER'] == 'synthetic-host-override',
                'host settings were not retained')
        require(receiver['restart'] == 'always', 'restart policy was not retained')
        isolated_model = json.loads(compose('config', '--format', 'json').stdout)
        require(isolated_model['services']['shirasu']['network_mode'] == 'none',
                'test receiver has network access')
        require(isolated_model['services']['shirasu']['restart'] == 'no',
                'test receiver can restart after cleanup')

        # From here cleanup is limited to this fresh, explicitly named project.
        cleanup_required = True
        saved_webp = probe('seed', 'webp')
        require(len(saved_webp) == 1, 'expected one seeded WebP event')
        webp = next(iter(saved_webp.values()))
        require('/v5/' in webp['url'] and webp['url'].endswith('.w1.webp')
                and webp['mime'] == 'image/webp', 'seed did not use WebP settings')
        stages = []

        def running_stage(label, version, encoding, expected, *, restart=False):
            if restart:
                compose('restart', 'shirasu')
            else:
                compose('up', '-d', '--no-build', '--pull', 'never', '--no-deps',
                        '--force-recreate', 'shirasu')
            deadline = time.monotonic() + 30
            while True:
                current = inspect()
                require(current['State']['Running'], 'receiver exited during startup')
                status = probe('status', running=True)
                if status['status'].get('running'):
                    break
                require(time.monotonic() < deadline, 'receiver did not save its running state')
                time.sleep(0.2)
            actual = probe('settings', running=True)
            require(actual == {'version': version, 'encoding': encoding,
                               'mime': 'image/webp' if encoding == 'w1' else 'image/png',
                               'marker': 'synthetic-host-override'}, 'receiver settings differ')
            require(current['Image'] == image_id and current['HostConfig']['NetworkMode'] == 'none',
                    'running receiver differs from the fixed, isolated image')
            require(current['Config']['Cmd'] == ['python', './main.py'], 'real receiver entrypoint was replaced')
            require(current['Config']['Labels']['com.docker.compose.project'] == project,
                    'receiver belongs to another Compose project')
            mounts = {mount['Destination']: mount for mount in current['Mounts']}
            require(mounts['/data']['Name'] == volume_name
                    and mounts['/app/config.py']['Source'] == str(paths['config.py'])
                    and not mounts['/app/config.py']['RW'], 'receiver lost its original mounts')
            health = compose('exec', '-T', 'shirasu', 'python', '-B', 'main.py', '--health', accepted=(1,))
            require(health.returncode == 1 and not status['status'].get('relay_connected')
                    and not status['status'].get('gpsd_connected'), 'isolated health was unexpectedly healthy')
            require(probe('events', running=True) == expected, 'restart changed signed saved events')
            compose('stop', 'shirasu', timeout=40)
            stopped = inspect()
            require(not stopped['State']['Running'] and stopped['State']['ExitCode'] == 0,
                    'receiver failed to shut down cleanly')
            require(probe('status')['status'].get('running') is False,
                    'receiver did not persist its stopped state')
            require(probe('events') == expected, 'shutdown changed signed saved events')
            stages.append({'label': label, 'version': version, 'encoding': encoding,
                           'container_id': current['Id'], 'image_id': current['Image'],
                           'state_volume': volume_name, 'started': True, 'exit_code': 0,
                           'health_exit_code': 1, 'saved_events_unchanged': True,
                           'event_ids': sorted(expected)})

        running_stage('webp', 'v5', 'w1', saved_webp)
        settings('v4', 'png')
        saved_both = probe('seed', 'png')
        require(len(saved_both) == 2 and saved_both.get(webp['event_id']) == webp,
                'PNG cutback rewrote the saved WebP event')
        png, = [event for event_id, event in saved_both.items() if event_id not in saved_webp]
        require('/v4/' in png['url'] and png['url'].endswith('.png') and png['mime'] == 'image/png',
                'new event after cutback did not use PNG')
        running_stage('png-cutback', 'v4', 'png', saved_both)
        running_stage('png-restart', 'v4', 'png', saved_both, restart=True)
        require(verify_bundle(bundle) == before, 'rehearsal changed the fixed candidate')
        result = {
            'schema_version': 1, 'mode': 'release-compose',
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'bundle_manifest_sha256': before['manifest_sha256'], 'image_id': image_id,
            'project': project, 'stages': stages, 'events': saved_both,
            'checks': {'fixed_image_all_services': True, 'build_removed': True,
                       'host_override_retained': True, 'project_and_state_retained': True,
                       'config_read_only': True, 'real_receiver_entrypoint': True,
                       'real_settings_and_event_signing': True, 'webp_to_png': True,
                       'saved_event_json_id_signature_unchanged': True,
                       'clean_stop_and_restart': True, 'no_network': True,
                       'candidate_unchanged': True},
            'scope': 'Synthetic config and new state volume only; real receiver with network none. '
                     'Health failure is expected without GPSD/relay. No live state, deployment or external posts.',
        }
        completed = True
    finally:
        if cleanup_required:
            compose('down', '--volumes', '--remove-orphans', '--timeout', '20', timeout=60)
        if completed:
            shutil.rmtree(root)
        else:
            print('Rehearsal diagnostics retained at: ' + str(root), file=sys.stderr)
    result['checks']['isolated_project_removed'] = True
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = check(args.bundle, args.image, args.output)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Compose rehearsal failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps({'mode': result['mode'], 'image_id': result['image_id'],
                      'output': str(args.output)}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
