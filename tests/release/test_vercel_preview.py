"""Main previews upload only checked image bytes and gate completion on live HTTPS."""

from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from shirasu.images.protocol import load_keys
from image_service.app import _runtime_keys
from tools.release import publish_vercel_preview as preview
from tools.release import vercel_api as deployment

SOURCE = 'a' * 40
MANIFEST_SHA = 'b' * 64
ENV = {'VERCEL_PREVIEW_ENABLED': 'true', 'GITHUB_ACTIONS': 'true',
       'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'push',
       'GITHUB_REPOSITORY': 'Example/Shirasu', 'GITHUB_SHA': SOURCE,
       'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
       'VERCEL_ORG_ID': 'team_example', 'VERCEL_PROJECT_ID': 'prj_example',
       'VERCEL_TOKEN': 'test-token-not-for-output',
       'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://pipelines.actions.githubusercontent.com/job/idtoken?api-version=2.0',
       'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'runtime-credential-not-for-output'}


class VercelPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.check, self.output = self.root / 'checks', self.root / 'result.json'
        contents = {'images/api/image.py': b'from image_service.app import app\n',
                    'images/image_service/app.py': b'IMAGE_CHECK_KEYS_VERSION = 1\n',
                    'images/vercel.json': b'{"rewrites": []}\n',
                    'receiver/shirasu/service.py': b'receiver source excluded from upload\n'}
        for name, data in contents.items():
            path = self.check / 'candidate' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.candidate = {'source': {'commit': SOURCE, 'working_tree': False},
                          'manifest_sha256': MANIFEST_SHA,
                          'files': {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
        self.evidence = {'source_commit': SOURCE, 'source_dirty': False,
                         'candidate_manifest_sha256': MANIFEST_SHA}
        self.project = {'id': ENV['VERCEL_PROJECT_ID'], 'accountId': ENV['VERCEL_ORG_ID'], 'name': 'shirasu-images'}
        self.deployed = {'id': 'dpl_example', 'projectId': ENV['VERCEL_PROJECT_ID'], 'target': None,
                         'readyState': 'READY', 'url': 'shirasu-images-example.vercel.app'}
        self.requests, self.body = [], None
        self.building = False

    def verifiers(self):
        return (patch.object(deployment, 'verify_bundle', return_value=self.candidate),
                patch.object(deployment, 'verify_public', return_value=self.evidence))

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if path == '/v9/projects/prj_example':
            return self.project
        if path == '/v2/files':
            self.assertEqual(kwargs['headers']['x-vercel-digest'], hashlib.sha1(kwargs['data']).hexdigest())
            return None
        if path == '/v13/deployments':
            self.body = json.loads(kwargs['data'])
            return dict(self.deployed, meta=self.body['meta'], readyState='BUILDING' if self.building else 'READY')
        if path == '/v13/deployments/dpl_example':
            return dict(self.deployed, meta=self.body['meta'])
        self.fail('Unexpected API route: ' + path)

    def test_only_enabled_main_push_or_manual_is_accepted(self):
        self.assertEqual(preview.context(ENV)['sha'], SOURCE)
        self.assertEqual(preview.context(dict(ENV, GITHUB_EVENT_NAME='workflow_dispatch'))['sha'], SOURCE)
        for changes in ({'VERCEL_PREVIEW_ENABLED': ''}, {'GITHUB_EVENT_NAME': 'pull_request'},
                        {'GITHUB_EVENT_NAME': 'pull_request_target'}, {'GITHUB_REF': 'refs/heads/topic'},
                        {'GITHUB_REF': 'refs/tags/images-v5-r1'}, {'GITHUB_ACTIONS': 'false'},
                        {'GITHUB_SHA': 'not-a-commit'}, {'VERCEL_ORG_ID': 'team_other?query'}):
            with self.subTest(changes=changes), patch.object(deployment, 'VercelAPI') as api:
                with self.assertRaises(preview.PreviewError):
                    preview.publish(self.check, self.output, environment=dict(ENV, **changes))
                api.assert_not_called()

    def test_provenance_mismatches_fail_before_api(self):
        cases = [({'source': {'commit': 'c' * 40, 'working_tree': False}}, {}),
                 ({'source': {'commit': SOURCE, 'working_tree': True}}, {}),
                 ({}, {'source_dirty': True}), ({}, {'source_commit': 'c' * 40}),
                 ({}, {'candidate_manifest_sha256': 'd' * 64})]
        for candidate_changes, evidence_changes in cases:
            with self.subTest(changes=(candidate_changes, evidence_changes)), \
                    patch.object(deployment, 'verify_bundle', return_value=dict(self.candidate, **candidate_changes)), \
                    patch.object(deployment, 'verify_public', return_value=dict(self.evidence, **evidence_changes)), \
                    patch.object(deployment, 'VercelAPI') as api:
                with self.assertRaises(preview.PreviewError):
                    preview.publish(self.check, self.output, environment=ENV)
                api.assert_not_called()

    def test_file_tampering_and_changed_evidence_fail_before_api(self):
        candidate, public = self.verifiers()
        with candidate, patch.object(deployment, 'verify_public', side_effect=[self.evidence, dict(self.evidence, source_dirty=True)]):
            with self.assertRaisesRegex(preview.PreviewError, 'changed while capturing'):
                preview.snapshot(self.check, preview.context(ENV))
        (self.check / 'candidate/images/api/image.py').write_bytes(b'changed')
        with candidate, public, patch.object(deployment, 'VercelAPI') as api:
            with self.assertRaisesRegex(preview.PreviewError, 'changed after verification'):
                preview.publish(self.check, self.output, environment=ENV)
            api.assert_not_called()
        linked = self.root / 'linked'
        linked.symlink_to(self.check, target_is_directory=True)
        with self.assertRaisesRegex(preview.PreviewError, 'Linked'):
            preview.snapshot(linked, preview.context(ENV))

    def test_only_checked_image_files_and_ephemeral_key_reach_preview(self):
        candidate, public = self.verifiers()
        with candidate, public, patch.object(deployment.VercelAPI, 'request', side_effect=self.request), \
                patch.object(deployment, 'verify_http', return_value={'state': 'success', 'images': 95}) as check:
            result = preview.publish(self.check, self.output, environment=ENV)
        self.assertEqual(result['state'], 'success')
        self.assertEqual(result['file_count'], 3)
        self.assertEqual(json.loads(self.output.read_text()), result)
        self.assertEqual(set(self.body), {'name', 'project', 'files', 'meta', 'env'})
        self.assertEqual(set(self.body['env']), {'SHIRASU_IMAGE_CHECK_KEYS'})
        keyring = json.loads(self.body['env']['SHIRASU_IMAGE_CHECK_KEYS'])
        self.assertRegex(next(iter(keyring)), r'^check-[0-9a-f]{10}$')
        # Exercise the actual signing/runtime boundary, not just the API mock.
        # An overlong generated ID would otherwise make every request a 503.
        auxiliary = load_keys(self.body['env']['SHIRASU_IMAGE_CHECK_KEYS'])
        with patch.dict(os.environ, {'SHIRASU_IMAGE_KEYS': json.dumps({'primary': '11' * 32}),
                                    **self.body['env']}, clear=True):
            self.assertEqual(_runtime_keys(), {'primary': bytes.fromhex('11' * 32), **auxiliary})
        self.assertEqual(check.call_args.kwargs['environment'], {
            'SHIRASU_IMAGE_KEYS': self.body['env']['SHIRASU_IMAGE_CHECK_KEYS']})
        self.assertEqual(check.call_args.kwargs['phase'], 'candidate')
        self.assertIsInstance(check.call_args.kwargs['oidc'], deployment.GitHubOIDC)
        for value in [ENV['VERCEL_TOKEN'], 'temporary_share_secret', *keyring.values()]:
            self.assertNotIn(value, self.output.read_text())
        self.assertFalse(any(b'receiver source' in kwargs.get('data', b'') for _, _, kwargs in self.requests))
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.requests))

    def test_candidate_failure_records_redacted_failure(self):
        candidate, public = self.verifiers()
        with candidate, public, patch.object(deployment.VercelAPI, 'request', side_effect=self.request), \
                patch.object(deployment, 'verify_http', side_effect=ValueError('sensitive image key')):
            with self.assertRaises(ValueError):
                preview.publish(self.check, self.output, environment=ENV)
        recorded = json.loads(self.output.read_text())
        self.assertEqual(recorded['state'], 'failed')
        self.assertEqual(recorded['failure'], 'ValueError')
        self.assertNotIn('sensitive', self.output.read_text())

    def test_project_owner_mismatch_prevents_upload(self):
        self.project['accountId'] = 'team_other'
        candidate, public = self.verifiers()
        with candidate, public, patch.object(deployment.VercelAPI, 'request', side_effect=self.request):
            with self.assertRaisesRegex(preview.PreviewError, 'requested team'):
                preview.publish(self.check, self.output, environment=ENV)
        self.assertEqual(len(self.requests), 1)

    def test_wrong_targets_and_identity_are_rejected(self):
        for changes in ({'target': 'production'}, {'target': 'staging'}, {'projectId': 'prj_other'},
                        {'readyState': 'ERROR'}, {'id': 'https://bad.example'}):
            with self.subTest(changes=changes), self.assertRaises(preview.PreviewError):
                deployment.deployment_state(dict(self.deployed, **changes), preview.context(ENV), 'preview')
        with self.assertRaises(preview.PreviewError):
            deployment.deployment_state(self.deployed, preview.context(ENV), 'preview', 'dpl_other')

    def test_build_is_polled_and_bad_hostname_cannot_leak_into_report(self):
        self.building = True
        candidate, public = self.verifiers()
        with candidate, public, patch.object(deployment.VercelAPI, 'request', side_effect=self.request), \
                patch.object(deployment.time, 'sleep') as sleep, patch.object(deployment, 'verify_http', return_value={}):
            result = preview.publish(self.check, self.output, environment=ENV)
        sleep.assert_called_once_with(5)
        self.assertEqual(result['deployment_url'], 'https://' + self.deployed['url'])
        self.output.unlink()
        self.deployed['url'] = 'shirasu.vercel.app/v5/secret-signature.png'
        with candidate, public, patch.object(deployment.VercelAPI, 'request', side_effect=self.request), patch.object(deployment.time, 'sleep'):
            with self.assertRaisesRegex(preview.PreviewError, 'unexpected deployment URL'):
                preview.publish(self.check, self.output, environment=ENV)
        self.assertNotIn('secret-signature', self.output.read_text())

    def test_api_hides_errors_and_refuses_redirects_and_project_mutation(self):
        api = deployment.VercelAPI(ENV['VERCEL_TOKEN'], ENV['VERCEL_ORG_ID'])
        secret = 'private value and signed image URL'
        errors = [HTTPError('https://api.vercel.com', 403, secret, {}, io.BytesIO(secret.encode())),
                  HTTPError('https://bad.example/' + ENV['VERCEL_TOKEN'], 302, secret, {}, None), URLError(secret)]
        for error in errors:
            with self.subTest(error=type(error)), patch.object(api.opener, 'open', side_effect=error):
                with self.assertRaises(preview.PreviewError) as raised:
                    api.request('GET', '/v9/projects/prj_example')
                self.assertNotIn(secret, str(raised.exception))
                self.assertNotIn(ENV['VERCEL_TOKEN'], str(raised.exception))
        self.assertIsNone(deployment.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://bad.example'))
        with patch.object(api.opener, 'open') as opened:
            for method, path in [('PATCH', '/v9/projects/prj_example'), ('GET', 'https://bad.example'), ('POST', '/v13/deployments?other=1')]:
                with self.assertRaises(preview.PreviewError):
                    api.request(method, path)
            opened.assert_not_called()

    def test_api_fixed_origin_team_and_cli_error_redaction(self):
        api = deployment.VercelAPI(ENV['VERCEL_TOKEN'], ENV['VERCEL_ORG_ID'])
        with patch.object(api.opener, 'open', return_value=io.BytesIO(b'{}')) as opened:
            api.request('GET', '/v9/projects/prj_example')
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.vercel.com/v9/projects/prj_example?teamId=team_example')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + ENV['VERCEL_TOKEN'])
        for error in (ValueError, RuntimeError):
            output = io.StringIO()
            with patch.object(preview, 'publish', side_effect=error(ENV['VERCEL_TOKEN'])), redirect_stderr(output):
                self.assertEqual(preview.main(['--check-root', str(self.check), '--output', str(self.output)]), 1)
            self.assertNotIn(ENV['VERCEL_TOKEN'], output.getvalue())


if __name__ == '__main__':
    unittest.main()
