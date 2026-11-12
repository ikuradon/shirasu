"""Production promotion requires restored provenance and successful HTTPS checks."""

import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts import publish_vercel_release as release
from scripts import vercel_api as deployment

SOURCE = 'a' * 40
ENV = {'VERCEL_RELEASE_ENABLED': 'true', 'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/tags/images-v5-r2',
       'GITHUB_EVENT_NAME': 'push', 'GITHUB_REPOSITORY': 'Example/Shirasu', 'GITHUB_SHA': SOURCE,
       'GITHUB_RUN_ID': '124', 'GITHUB_RUN_ATTEMPT': '1', 'VERCEL_ORG_ID': 'team_example',
       'VERCEL_PROJECT_ID': 'prj_example', 'VERCEL_TOKEN': 'test-secret-token',
       'VERCEL_PRODUCTION_DOMAIN': 'shirasu-images.vercel.app', 'RELEASE_TAG': 'images-v5-r2',
       'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://pipelines.actions.githubusercontent.com/job/idtoken?api-version=2.0',
       'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'runtime-credential-not-for-output'}


class GitHubOIDCTests(unittest.TestCase):
    @staticmethod
    def token(provider, **changes):
        payload = dict(provider.claims, iss='https://token.actions.githubusercontent.com',
                       aud=provider.audience, exp=1300)
        payload.update(changes)
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        return 'eyJhbGciOiJSUzI1NiJ9.' + encoded + '.testsignature'

    def test_fetches_expected_audience_and_refreshes_expiring_token_without_files(self):
        provider = deployment.GitHubOIDC(ENV)
        first, second = self.token(provider), self.token(provider, exp=1541)
        responses = [io.BytesIO(json.dumps({'value': token}).encode()) for token in (first, second)]
        with patch.object(provider.opener, 'open', side_effect=responses) as opened, patch.object(deployment.time, 'time', return_value=1000) as clock:
            self.assertEqual(provider(), first)
            self.assertEqual(provider(), first)
            self.assertEqual(opened.call_count, 1)
            clock.return_value = 1241
            self.assertEqual(provider(), second)
            self.assertEqual(opened.call_count, 2)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, ENV['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=https%3A%2F%2Fgithub.com%2FExample')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + ENV['ACTIONS_ID_TOKEN_REQUEST_TOKEN'])

    def test_request_credentials_cannot_go_to_foreign_or_ambiguous_url(self):
        for url in ('http://pipelines.actions.githubusercontent.com/token', 'https://evil.example/token',
                    'https://pipelines.actions.githubusercontent.com.evil.example/token',
                    'https://user:pass@pipelines.actions.githubusercontent.com/token',
                    'https://pipelines.actions.githubusercontent.com:8443/token',
                    'https://pipelines.actions.githubusercontent.com/token#fragment',
                    'https://pipelines.actions.githubusercontent.com/token?audience=elsewhere'):
            with self.subTest(url=url), self.assertRaises(deployment.DeploymentError):
                deployment.GitHubOIDC(dict(ENV, ACTIONS_ID_TOKEN_REQUEST_URL=url))
        for changes in ({'ACTIONS_ID_TOKEN_REQUEST_TOKEN': ''}, {'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'bad\r\nheader'},
                        {'GITHUB_ACTIONS': 'false'}, {'GITHUB_RUN_ID': ''}):
            with self.subTest(changes=changes), self.assertRaises(deployment.DeploymentError):
                deployment.GitHubOIDC(dict(ENV, **changes))

    def test_wrong_claims_expiration_and_corrupt_response_fail_closed(self):
        provider = deployment.GitHubOIDC(ENV)
        for changes in ({'iss': 'https://elsewhere.test'}, {'aud': 'different audience'}, {'repository': 'Other/Repo'},
                        {'sha': 'b' * 40}, {'ref': 'refs/heads/other'}, {'run_id': '999'},
                        {'run_attempt': '2'}, {'exp': 1000}, {'exp': 90000}, {'exp': True}):
            response = io.BytesIO(json.dumps({'value': self.token(provider, **changes)}).encode())
            with self.subTest(changes=changes), patch.object(provider.opener, 'open', return_value=response), \
                    patch.object(deployment.time, 'time', return_value=1000), self.assertRaises(deployment.DeploymentError):
                provider()
        for raw in (b'not-json', b'{"value":"unstructured-private-token"}', b'{"value":"a.Zm9v.b"}', b'x' * 65537):
            with self.subTest(raw=raw[:20]), patch.object(provider.opener, 'open', return_value=io.BytesIO(raw)), \
                    self.assertRaises(deployment.DeploymentError) as raised:
                provider()
            self.assertNotIn('private-token', str(raised.exception))

    def test_token_endpoint_failure_and_redirect_are_redacted(self):
        provider = deployment.GitHubOIDC(ENV)
        secret = ENV['ACTIONS_ID_TOKEN_REQUEST_TOKEN']
        for error in (HTTPError('https://other.example/' + secret, 302, secret, {}, None), URLError(secret)):
            with self.subTest(error=type(error)), patch.object(provider.opener, 'open', side_effect=error), \
                    self.assertRaises(deployment.DeploymentError) as raised:
                provider()
            self.assertNotIn(secret, str(raised.exception))


class VercelDeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle, self.public = self.root / 'candidate', self.root / 'public'
        self.bundle.mkdir()
        self.public.mkdir()
        manifest = {'schema_version': 1, 'candidate_version': 'v5', 'source': {'commit': SOURCE, 'working_tree': False},
                    'files': {'images/api/image.py': 'e' * 64}}
        raw = json.dumps(manifest).encode()
        (self.bundle / 'candidate-manifest.json').write_bytes(raw)
        self.candidate = dict(manifest, manifest_sha256=hashlib.sha256(raw).hexdigest())
        self.receipt = {'schema_version': 2, 'version': 'v5', 'release_tag': 'images-v5-r2',
                        'platform': 'linux/arm64', 'source_commit': SOURCE,
                        'candidate_manifest_sha256': self.candidate['manifest_sha256'],
                        'run': {'repository': 'Example/Shirasu', 'id': '123', 'attempt': '1'},
                        'images': {role: {'reference': 'ghcr.io/example/shirasu-' + suffix + '@sha256:' + 'c' * 64,
                                          'image_id': 'sha256:' + 'd' * 64}
                                   for role, suffix in [('receiver', 'receiver'), ('renderer', 'images')]}}
        self.restored = {name: self.receipt[name] for name in ('version', 'release_tag', 'platform', 'source_commit',
                                                             'candidate_manifest_sha256', 'images')}
        self.restored.update(schema_version=1, mode='ci-restore', publication_run=self.receipt['run'], file_count=1,
                             checks={name: True for name in release.RESTORE_CHECKS})
        self.receipt_path, self.restore_path, self.output = self.root / 'release.json', self.root / 'restore.json', self.root / 'result.json'
        self.write_metadata()
        self.body, self.calls, self.promoted, self.alias_reads = None, [], False, 0
        self.project_changes, self.deployment_changes, self.alias_changes = {}, {}, {}
        self.aliases = []
        self.checks, self.check_failure, self.racing = [], False, False

    def write_metadata(self):
        self.receipt_path.write_text(json.dumps(self.receipt))
        self.restore_path.write_text(json.dumps(self.restored))

    def api(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == '/v9/projects/prj_example':
            return dict({'id': 'prj_example', 'accountId': 'team_example', 'name': 'shirasu-images'}, **self.project_changes)
        if path == '/v4/aliases/shirasu-images.vercel.app':
            self.alias_reads += 1
            identity = 'dpl_new' if self.promoted else 'dpl_old'
            if self.racing and self.alias_reads > 1:
                identity = 'dpl_concurrent'
            return dict({'alias': 'shirasu-images.vercel.app', 'projectId': 'prj_example', 'deploymentId': identity}, **self.alias_changes)
        if path == '/v2/files':
            return None
        if path == '/v13/deployments':
            self.body = json.loads(kwargs['data'])
        if path in ('/v13/deployments', '/v13/deployments/dpl_new'):
            return dict({'id': 'dpl_new', 'projectId': 'prj_example', 'target': 'production', 'readyState': 'READY',
                         'url': 'shirasu-images-candidate.vercel.app', 'meta': self.body['meta']}, **self.deployment_changes)
        if path == '/v2/deployments/dpl_new/aliases':
            return {'aliases': self.aliases}
        if path == '/v10/projects/prj_example/promote/dpl_new':
            self.promoted = True
            return {}
        self.fail('Unexpected request: ' + path)

    def http(self, bundle, public, url, report, *, phase, environment, oidc=None):
        self.checks.append((url, phase, environment, oidc))
        if self.check_failure:
            raise ValueError('do not output key or signed URL')
        return {'state': 'success', 'images': 95}

    def publish(self, environment=None):
        with patch.object(deployment, 'snapshot', return_value=(self.candidate, [({'file': 'api/image.py', 'sha': 'f' * 40, 'size': 4}, b'app\n')])), \
                patch.object(deployment.VercelAPI, 'request', side_effect=self.api), \
                patch.object(deployment, 'verify_http', side_effect=self.http), \
                patch.object(release, 'current_release') as current:
            result = release.publish(self.bundle, self.public, self.receipt_path, self.restore_path,
                                     self.output, environment=ENV if environment is None else environment)
        return result, current

    def test_restored_candidate_is_checked_then_same_production_build_promoted(self):
        result, current = self.publish()
        self.assertEqual(result['state'], 'success')
        self.assertEqual(result['deployment_id'], 'dpl_new')
        self.assertEqual(result['previous_deployment_id'], 'dpl_old')
        self.assertEqual(self.body['target'], 'production')
        self.assertIs(self.body['autoAssignCustomDomains'], False)
        self.assertEqual(set(self.body['env']), {'SHIRASU_IMAGE_CHECK_KEYS'})
        self.assertEqual(self.body['meta']['shirasuReleaseTag'], 'images-v5-r2')
        self.assertEqual([row[1] for row in self.checks], ['candidate', 'production'])
        candidate_env, production_env = self.checks[0][2], self.checks[1][2]
        self.assertIsInstance(self.checks[0][3], deployment.GitHubOIDC)
        self.assertIsNone(self.checks[1][3])
        self.assertEqual(set(candidate_env), {'SHIRASU_IMAGE_KEYS'})
        self.assertEqual(set(production_env), {'SHIRASU_IMAGE_KEYS'})
        self.assertEqual(production_env['SHIRASU_IMAGE_KEYS'], self.body['env']['SHIRASU_IMAGE_CHECK_KEYS'])
        self.assertEqual(current.call_count, 2)
        current.assert_called_with('Example/Shirasu', 'images-v5-r2', SOURCE)
        self.assertTrue(self.promoted)
        promotions = [call for call in self.calls if '/promote/' in call[1]]
        self.assertEqual(len(promotions), 1)
        self.assertIs(promotions[0][2]['expect_json'], False)
        key = next(iter(json.loads(production_env['SHIRASU_IMAGE_KEYS']).values()))
        for secret in (key, ENV['VERCEL_TOKEN'], 'temporary_share_secret'):
            self.assertNotIn(secret, self.output.read_text())
        self.assertFalse(any(method == 'PATCH' and '/projects/' in path for method, path, _ in self.calls))

    def test_candidate_http_failure_never_promotes(self):
        self.check_failure = True
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(self.promoted)
        result = json.loads(self.output.read_text())
        self.assertEqual(result['stage'], 'candidate-http')
        self.assertEqual(result['failure'], 'ValueError')
        self.assertNotIn('signed URL', self.output.read_text())

    def test_preassigned_alias_wrong_target_or_metadata_never_passes(self):
        cases = [('aliases', [{'alias': ENV['VERCEL_PRODUCTION_DOMAIN']}]),
                 ('deployment_changes', {'target': None}), ('deployment_changes', {'projectId': 'prj_other'}),
                 ('deployment_changes', {'meta': {'shirasuSourceCommit': SOURCE}})]
        for attribute, value in cases:
            with self.subTest(attribute=attribute, value=value):
                setattr(self, attribute, value)
                with self.assertRaises(deployment.DeploymentError):
                    self.publish()
                self.assertFalse(self.promoted)
                self.assertFalse(self.checks)
                setattr(self, attribute, [] if attribute == 'aliases' else {})
                self.output.unlink()

    def test_wrong_project_or_domain_owner_fails_before_upload(self):
        for attribute, value in [('project_changes', {'accountId': 'team_other'}),
                                 ('alias_changes', {'projectId': 'prj_other'})]:
            with self.subTest(attribute=attribute):
                setattr(self, attribute, value)
                with self.assertRaises(deployment.DeploymentError):
                    self.publish()
                self.assertIsNone(self.body)
                setattr(self, attribute, {})
                self.output.unlink()

    def test_concurrent_production_change_prevents_promotion(self):
        self.racing = True
        with self.assertRaisesRegex(deployment.DeploymentError, 'Production changed'):
            self.publish()
        self.assertFalse(self.promoted)

    def test_latest_release_guard_blocks_promotion(self):
        original = release.current_release
        with patch.object(deployment, 'snapshot', return_value=(self.candidate, [])), \
                patch.object(deployment.VercelAPI, 'request', side_effect=self.api), \
                patch.object(deployment, 'verify_http', side_effect=self.http), \
                patch.object(release, 'current_release', side_effect=ValueError('newer release exists')):
            with self.assertRaises(ValueError):
                release.publish(self.bundle, self.public, self.receipt_path, self.restore_path, self.output, environment=ENV)
        self.assertFalse(self.promoted)
        self.assertIs(release.current_release, original)

    def test_manual_run_can_use_new_workflow_for_fixed_older_source(self):
        environment = dict(ENV, GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_SHA='c' * 40)
        self.assertEqual(release.context(environment, self.receipt)['sha'], 'c' * 40)
        result, _ = self.publish(environment)
        self.assertEqual(result['workflow_commit'], 'c' * 40)
        self.assertEqual(result['source_commit'], SOURCE)

    def test_pr_wrong_tag_and_unenabled_context_are_rejected(self):
        for changes in ({'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_EVENT_NAME': 'pull_request_target'},
                        {'GITHUB_REF': 'refs/heads/main'}, {'GITHUB_SHA': 'c' * 40},
                        {'RELEASE_TAG': 'images-v5-r1'}, {'VERCEL_RELEASE_ENABLED': ''},
                        {'GITHUB_REPOSITORY': 'Other/Shirasu'}):
            with self.subTest(changes=changes), self.assertRaises(deployment.DeploymentError):
                release.context(dict(ENV, **changes), self.receipt)

    def test_restore_mismatch_fails_before_api(self):
        for changes in ({'source_commit': 'c' * 40}, {'file_count': 2}, {'checks': {}},
                        {'publication_run': {'id': '999'}}, {'images': {}}, {'mode': 'local'}):
            with self.subTest(changes=changes):
                self.restore_path.write_text(json.dumps(dict(self.restored, **changes)))
                with patch.object(deployment, 'VercelAPI') as api, self.assertRaises(deployment.DeploymentError):
                    release.publish(self.bundle, self.public, self.receipt_path, self.restore_path, self.output, environment=ENV)
                api.assert_not_called()

    def test_old_distribution_check_contract_and_ambiguous_markers_rejected(self):
        for source in (b'print("old distribution")', b'IMAGE_CHECK_KEYS_VERSION = True',
                       b'IMAGE_CHECK_KEYS_VERSION = 2', b'IMAGE_CHECK_KEYS_VERSION = 1\nIMAGE_CHECK_KEYS_VERSION = 1',
                       b'if True:\n IMAGE_CHECK_KEYS_VERSION = 1', b'IMAGE_CHECK_KEYS_VERSION = ('):
            with self.subTest(source=source), self.assertRaises(deployment.DeploymentError):
                deployment.check_key_support([({'file': 'image_service/app.py'}, source)])
        deployment.check_key_support([({'file': 'image_service/app.py'}, b'IMAGE_CHECK_KEYS_VERSION = 1')])

    def test_http_gate_requires_95_successful_images(self):
        from scripts import check_vercel_release as checker
        report = self.root / 'http.json'
        report.write_text('{}')
        for state, images in [('failed', 95), ('success', 94)]:
            with self.subTest(state=state, images=images), patch.object(checker, 'check', return_value={
                    'state': state, 'images': [{}] * images, 'requests': []}), self.assertRaises(deployment.DeploymentError):
                deployment.verify_http(self.bundle, self.public, 'https://host.vercel.app', report,
                                       phase='candidate', environment={})
        with patch.object(checker, 'check', return_value={'state': 'success', 'images': [{}] * 95, 'requests': [{}] * 100}):
            result = deployment.verify_http(self.bundle, self.public, 'https://host.vercel.app', report,
                                            phase='candidate', environment={})
        self.assertEqual(result['requests'], 100)
        self.assertEqual(result['sha256'], hashlib.sha256(report.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
