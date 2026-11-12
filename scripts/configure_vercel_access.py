#!/usr/bin/env python3
"""Add this repository's exact Actions OIDC rules without replacing other trust."""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts import vercel_api as deployment

ISSUER = 'https://token.actions.githubusercontent.com'
RULES = ROOT / 'docs/vercel-trusted-sources.json'


class AccessAPI(deployment.VercelAPI):
    def update_trusted_sources(self, project_id, trusted_sources):
        """The only mutation this setup tool exposes is this one project field."""
        deployment.require(isinstance(project_id, str) and re.fullmatch(r'prj_[A-Za-z0-9]+', project_id),
                           'Invalid Vercel project identity')
        deployment.require(isinstance(trusted_sources, dict), 'Expected a Trusted Sources object')
        body = json.dumps({'trustedSources': trusted_sources}).encode()
        deployment.require(len(body) <= 1024 * 1024, 'Trusted Sources configuration is too large')
        request = Request('https://api.vercel.com/v9/projects/' + project_id + '?'
                          + urlencode({'teamId': self.team_id}), method='PATCH', data=body,
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=120) as response:
                # Do not log or retain the project response, which can include
                # environment metadata unrelated to this operation.
                response.read(1024 * 1024 + 1)
        except HTTPError as error:
            raise deployment.DeploymentError('Trusted Sources update failed (HTTP ' + str(error.code) + ')') from None
        except (URLError, OSError, ValueError):
            raise deployment.DeploymentError('Trusted Sources update failed; credentials omitted') from None


def expected_rules(repository):
    common = {'aud': ['https://github.com/' + repository.split('/')[0]], 'repository': [repository]}
    events = ['push', 'workflow_dispatch']
    return {ISSUER: [
        {'claims': {**common, 'ref': ['refs/heads/main'], 'workflow': ['Tests'], 'event_name': events},
         'to': {'slugs': ['preview']}},
        {'claims': {**common, 'environment': ['vercel-production'], 'event_name': events,
                    'workflow': ['Publish verified image release', 'Deploy verified release to Vercel']},
         'to': {'slugs': ['production']}},
    ]}


def load_rules(path, repository):
    path = Path(path).absolute()
    deployment.require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
                       'Expected an unlinked OIDC rules file')
    with path.open('rb') as stream:
        raw = stream.read(16385)
    deployment.require(len(raw) <= 16384, 'OIDC rules file is too large')
    try:
        rules = json.loads(raw)
    except (ValueError, UnicodeError):
        raise deployment.DeploymentError('Invalid OIDC rules JSON') from None
    deployment.require(rules == expected_rules(repository),
                       'OIDC rules must match this repository and the two approved workflows')
    return rules


def read_project(api, ctx):
    project = api.request('GET', '/v9/projects/' + ctx['project_id'])
    deployment.require(isinstance(project, dict) and project.get('id') == ctx['project_id']
                       and project.get('accountId') == ctx['team_id'],
                       'Vercel project does not belong to the requested team')
    trusted = project.get('trustedSources')
    deployment.require(trusted is None or isinstance(trusted, dict), 'Invalid existing Trusted Sources configuration')
    return project


def merge_rules(trusted, rules):
    merged = copy.deepcopy(trusted or {})
    providers = merged.get('oidcProviders')
    deployment.require(providers is None or isinstance(providers, dict), 'Invalid existing OIDC providers')
    if providers is None:
        providers = merged['oidcProviders'] = {}
    added = 0
    for issuer, requested in rules.items():
        existing = providers.setdefault(issuer, [])
        deployment.require(isinstance(existing, list) and all(isinstance(rule, dict) for rule in existing),
                           'Invalid existing OIDC rules')
        for rule in requested:
            if rule not in existing:
                existing.append(copy.deepcopy(rule))
                added += 1
    return merged, added


def contains_settings(actual, expected):
    """Allow server-added defaults, but require every preserved setting exactly."""
    return isinstance(actual, dict) and all(key in actual and actual[key] == value for key, value in expected.items())


def configure(output, *, rules_path=RULES, environment=None):
    env = os.environ if environment is None else environment
    deployment.require(env.get('GITHUB_REF') == 'refs/heads/main'
                       and env.get('GITHUB_EVENT_NAME') in ('push', 'workflow_dispatch'),
                       'Trusted Sources setup requires a main push or manual main run')
    ctx = deployment.actions_identity(env)
    rules = load_rules(rules_path, ctx['repository'])
    output, _, _ = deployment.output_paths(output, (Path(rules_path).absolute().parent,))
    api = AccessAPI(env.get('VERCEL_TOKEN', ''), ctx['team_id'])
    result = {'schema_version': 1, 'state': 'running', 'project_id': ctx['project_id'],
              'team_id': ctx['team_id'], 'repository': ctx['repository'], 'added_rules': 0}
    try:
        before = read_project(api, ctx)
        merged, added = merge_rules(before.get('trustedSources'), rules)
        if added:
            # Re-read immediately before the replacing PATCH so a change made
            # while preparing the merge is not silently discarded.
            fresh = read_project(api, ctx)
            deployment.require(fresh.get('trustedSources') == before.get('trustedSources')
                               and ('trustedSources' in fresh) == ('trustedSources' in before),
                               'Trusted Sources changed concurrently; retry the setup')
            api.update_trusted_sources(ctx['project_id'], merged)
        after = read_project(api, ctx)
        deployment.require(contains_settings(after.get('trustedSources'), merged),
                           'Trusted Sources read-back differs; inspect the project settings')
        result.update(state='success', added_rules=added)
    except Exception as error:
        deployment.failure(result, error)
        raise
    finally:
        deployment.record(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rules', type=Path, default=RULES)
    args = parser.parse_args(argv)
    try:
        result = configure(args.output, rules_path=args.rules)
    except deployment.DeploymentError as error:
        print('Vercel access setup failed: ' + str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Vercel access setup failed; credentials and project contents omitted', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
