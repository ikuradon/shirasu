#!/usr/bin/env python3
"""Stage a restored release, verify HTTPS, promote that build, then verify publicly."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts import vercel_api as deployment
from scripts.prepare_release_candidate import MANIFEST_NAME
from scripts.prepare_vercel_release import read_metadata, RESTORE_CHECKS
from scripts.release_identity import receipt_tag
from scripts.restore_release_candidate import read_json, validate_receipt
from scripts.verify_ci_restore import current_release


def context(environment, receipt):
    deployment.require(environment.get('VERCEL_RELEASE_ENABLED') == 'true', 'Release publication is disabled')
    ctx = deployment.actions_identity(environment)
    tag = receipt_tag(receipt)
    event, ref = environment.get('GITHUB_EVENT_NAME'), environment.get('GITHUB_REF')
    automatic = event == 'push' and ref == 'refs/tags/' + tag and ctx['sha'] == receipt['source_commit']
    manual = event == 'workflow_dispatch' and ref == 'refs/heads/main'
    deployment.require(automatic or manual, 'Release publication requires its formal tag or a manual main run')
    deployment.require(environment.get('RELEASE_TAG') == tag
                       and ctx['repository'].lower() == receipt['run']['repository'].lower(),
                       'Requested release tag or repository differs from the receipt')
    return ctx


def release_inputs(bundle, release, restore_result):
    receipt = read_json(read_metadata(release))
    manifest = validate_receipt(receipt, read_metadata(Path(bundle) / MANIFEST_NAME))
    restored = read_json(read_metadata(restore_result))
    deployment.require(isinstance(restored, dict) and type(restored.get('schema_version')) is int
                       and restored['schema_version'] == 1 and restored.get('mode') == 'ci-restore',
                       'Missing successful separate-runner restore report')
    for name in ('version', 'source_commit', 'release_tag', 'platform', 'candidate_manifest_sha256', 'images'):
        deployment.require(restored.get(name) == receipt.get(name), 'Restore report differs from release metadata')
    deployment.require(restored.get('publication_run') == receipt['run']
                       and type(restored.get('file_count')) is int
                       and restored.get('file_count') == len(manifest['files'])
                       and isinstance(restored.get('checks'), dict)
                       and all(restored['checks'].get(name) is True for name in RESTORE_CHECKS),
                       'Release has not passed all required restoration checks')
    return receipt


def production_host(environment):
    host = environment.get('VERCEL_PRODUCTION_DOMAIN', '')
    deployment.require(isinstance(host, str) and re.fullmatch(
        r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+', host),
        'Expected the explicit production domain without a URL or path')
    return host


def assigned_deployment(api, ctx, host):
    result = api.request('GET', '/v4/aliases/' + host)
    identity = result.get('deploymentId') if isinstance(result, dict) else None
    deployment.require(isinstance(result, dict) and result.get('alias') == host
                       and result.get('projectId') == ctx['project_id']
                       and isinstance(identity, str) and re.fullmatch(r'dpl_[A-Za-z0-9]+', identity),
                       'Production domain does not belong to the requested project')
    return identity


def publish(bundle, public_artifact, release, restore_result, output, *, environment=None):
    environment = os.environ if environment is None else environment
    receipt = release_inputs(bundle, release, restore_result)
    ctx = context(environment, receipt)
    host = production_host(environment)
    output, candidate_report, production_report = deployment.output_paths(output, (bundle, public_artifact))
    candidate, files = deployment.snapshot(bundle, public_artifact, receipt['source_commit'])
    deployment.require(candidate['manifest_sha256'] == receipt['candidate_manifest_sha256'],
                       'Restored candidate differs from the published release')
    oidc = deployment.GitHubOIDC(environment)
    api = deployment.VercelAPI(environment.get('VERCEL_TOKEN', ''), ctx['team_id'])
    tag = receipt_tag(receipt)
    result = {'schema_version': 1, 'mode': 'release-production', 'target': 'production',
              'state': 'running', 'stage': 'build', 'release_tag': tag,
              'source_commit': receipt['source_commit'], 'workflow_commit': ctx['sha'],
              'candidate_manifest_sha256': candidate['manifest_sha256'],
              'team_id': ctx['team_id'], 'project_id': ctx['project_id'],
              'production_url': 'https://' + host,
              'run': {key: ctx[key] for key in ('repository', 'run_id', 'attempt')}}
    try:
        deployment.check_project(api, ctx)
        previous = assigned_deployment(api, ctx, host)
        result['previous_deployment_id'] = previous
        staged, key_environment = deployment.stage(api, ctx, candidate, files, target='production', release_tag=tag)
        result.update(staged)
        result['stage'] = 'candidate-http'
        result['candidate_check'] = deployment.verify_http(
            bundle, public_artifact, staged['deployment_url'], candidate_report,
            phase='candidate', environment=key_environment, oidc=oidc)
        # Recheck both the GitHub release and domain after the long build/check.
        result['stage'] = 'promote'
        deployment.unassigned(api, ctx, staged['deployment_id'])
        current_release(ctx['repository'], tag, receipt['source_commit'])
        deployment.require(assigned_deployment(api, ctx, host) == previous,
                           'Production changed while the candidate was being verified')
        api.request('POST', '/v10/projects/' + ctx['project_id'] + '/promote/' + staged['deployment_id'],
                    data=b'{}', headers={'Content-Type': 'application/json'}, expect_json=False)
        result['promotion_requested'] = True
        result['stage'] = 'domain-assignment'
        current_release(ctx['repository'], tag, receipt['source_commit'])
        deadline = time.monotonic() + 180
        while assigned_deployment(api, ctx, host) != staged['deployment_id']:
            deployment.require(time.monotonic() < deadline, 'Production domain assignment exceeded the wait limit')
            time.sleep(3)
        result['stage'] = 'production-http'
        result['production_check'] = deployment.verify_http(
            bundle, public_artifact, 'https://' + host, production_report,
            phase='production', environment=key_environment)
        deployment.require(assigned_deployment(api, ctx, host) == staged['deployment_id'],
                           'Production deployment changed during the public HTTP checks')
        result.update(state='success', stage='complete', build_state='READY')
    except Exception as error:
        deployment.failure(result, error)
        raise
    finally:
        deployment.record(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'public-artifact', 'release', 'restore-result', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = publish(args.bundle, args.public_artifact, args.release, args.restore_result, args.output)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
                stream.write('Vercel production: [' + result['release_tag'] + ']('
                             + result['production_url'] + ')\n\n95 candidate and 95 anonymous production '
                             'image HTTPS checks passed.\n\nDeployment: `' + result['deployment_id']
                             + '`\n\nCandidate manifest: `' + result['candidate_manifest_sha256'] + '`\n')
    except deployment.DeploymentError as error:
        print('Vercel release failed: ' + str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Vercel release failed; verify the release evidence and deployment settings', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
