#!/usr/bin/env python3
"""Publish a verified main candidate to Preview and check its protected HTTPS URL."""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from tools.release import vercel_api as deployment

PreviewError = deployment.DeploymentError


def context(environment):
    deployment.require(environment.get('VERCEL_PREVIEW_ENABLED') == 'true', 'Preview publication is disabled')
    deployment.require(environment.get('GITHUB_REF') == 'refs/heads/main'
                       and environment.get('GITHUB_EVENT_NAME') in ('push', 'workflow_dispatch'),
                       'Preview publication requires a main push or manual main run')
    return deployment.actions_identity(environment)


def snapshot(check_root, ctx):
    root = Path(check_root).absolute()
    return deployment.snapshot(root / 'candidate', root / 'public', ctx['sha'])


def publish(check_root, output, *, environment=None):
    environment = os.environ if environment is None else environment
    ctx = context(environment)
    root = Path(check_root).absolute()
    output, candidate_report, _ = deployment.output_paths(output, (root / 'candidate', root / 'public'))
    candidate, files = snapshot(root, ctx)
    oidc = deployment.GitHubOIDC(environment)
    api = deployment.VercelAPI(environment.get('VERCEL_TOKEN', ''), ctx['team_id'])
    result = {'schema_version': 1, 'mode': 'head-preview', 'target': 'preview',
              'state': 'running', 'stage': 'build', 'source_commit': ctx['sha'],
              'candidate_manifest_sha256': candidate['manifest_sha256'],
              'team_id': ctx['team_id'], 'project_id': ctx['project_id'],
              'run': {key: ctx[key] for key in ('repository', 'run_id', 'attempt')}}
    try:
        staged, key_environment = deployment.stage(api, ctx, candidate, files, target='preview')
        result.update(staged)
        result['stage'] = 'candidate-http'
        result['candidate_check'] = deployment.verify_http(
            root / 'candidate', root / 'public', staged['deployment_url'], candidate_report,
            phase='candidate', environment=key_environment, oidc=oidc)
        result.update(state='success', stage='complete', build_state='READY')
    except Exception as error:
        deployment.failure(result, error)
        raise
    finally:
        deployment.record(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = publish(args.check_root, args.output)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
                stream.write('Vercel Preview: [' + result['source_commit'][:7] + ']('
                             + result['deployment_url'] + ')\n\n95 image HTTPS checks passed.\n\n'
                             'Candidate manifest: `' + result['candidate_manifest_sha256'] + '`\n')
    except PreviewError as error:
        print('Vercel Preview publication failed: ' + str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Vercel Preview publication failed; check candidate and deployment settings', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
