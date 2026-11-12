"""Only exact repository-bound rules are added; existing trust is preserved."""

from contextlib import redirect_stderr
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tools.release import configure_vercel_access as access
from tools.release import vercel_api as deployment

ENV = {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'push',
       'GITHUB_REPOSITORY': 'Example/Shirasu', 'GITHUB_SHA': 'a' * 40,
       'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
       'VERCEL_ORG_ID': 'team_example', 'VERCEL_PROJECT_ID': 'prj_example',
       'VERCEL_TOKEN': 'private-vercel-token'}


class VercelAccessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rules_path = self.root / 'config/rules.json'
        self.rules_path.parent.mkdir()
        self.rules = access.expected_rules(ENV['GITHUB_REPOSITORY'])
        self.rules_path.write_text(json.dumps(self.rules))
        self.output = self.root / 'result.json'
        self.project = {'id': 'prj_example', 'accountId': 'team_example', 'name': 'shirasu-images',
                        'env': [{'value': 'unrelated-project-secret'}]}

    def configure(self, reads, update=None, environment=None):
        with patch.object(access.AccessAPI, 'request', side_effect=reads) as get, \
                patch.object(access.AccessAPI, 'update_trusted_sources', side_effect=update) as put:
            result = access.configure(self.output, rules_path=self.rules_path, environment=environment or ENV)
        return result, get, put

    def test_absent_configuration_is_added_and_read_back(self):
        after = dict(self.project, trustedSources={'oidcProviders': self.rules})
        result, _, put = self.configure([self.project, self.project, after])
        self.assertEqual(result['added_rules'], 2)
        self.assertEqual(result['state'], 'success')
        put.assert_called_once_with('prj_example', {'oidcProviders': self.rules})
        self.assertNotIn('private-vercel-token', self.output.read_text())
        self.assertNotIn('unrelated-project-secret', self.output.read_text())
        self.assertNotIn('claims', self.output.read_text())

    def test_existing_projects_other_issuers_and_same_issuer_rules_are_preserved(self):
        old_rule = {'claims': {'repository': ['other/repo']}, 'to': {'slugs': ['preview']}}
        trusted = {'projects': {'prj_kept': [{'from': {'slugs': ['production']}, 'to': {'slugs': ['preview']}}]},
                   'enableVercelCiSameRepository': False,
                   'oidcProviders': {'https://issuer.example.test': [old_rule], access.ISSUER: [old_rule]}}
        untouched = copy.deepcopy(trusted)
        merged, _ = access.merge_rules(trusted, self.rules)
        before, after = dict(self.project, trustedSources=trusted), dict(self.project, trustedSources=merged)
        result, _, put = self.configure([before, before, after])
        self.assertEqual(result['added_rules'], 2)
        self.assertEqual(trusted, untouched)
        self.assertEqual(merged['projects'], trusted['projects'])
        self.assertFalse(merged['enableVercelCiSameRepository'])
        self.assertEqual(put.call_args.args[1], merged)
        self.assertEqual(len(merged['oidcProviders'][access.ISSUER]), 3)

    def test_already_configured_is_read_only(self):
        project = dict(self.project, trustedSources={'oidcProviders': self.rules})
        result, get, put = self.configure([project, project])
        self.assertEqual(result['added_rules'], 0)
        self.assertEqual(get.call_count, 2)
        put.assert_not_called()

    def test_partially_configured_only_adds_missing_rule(self):
        trusted = {'oidcProviders': {access.ISSUER: [self.rules[access.ISSUER][0]]}}
        merged, added = access.merge_rules(trusted, self.rules)
        self.assertEqual(added, 1)
        self.assertEqual(merged['oidcProviders'], self.rules)

    def test_concurrent_change_stops_before_patch(self):
        other = dict(self.project, trustedSources={'projects': {'prj_new': []}})
        with patch.object(access.AccessAPI, 'request', side_effect=[self.project, other]), \
                patch.object(access.AccessAPI, 'update_trusted_sources') as put:
            with self.assertRaisesRegex(deployment.DeploymentError, 'changed concurrently'):
                access.configure(self.output, rules_path=self.rules_path, environment=ENV)
        put.assert_not_called()
        self.assertEqual(json.loads(self.output.read_text())['state'], 'failed')

    def test_readback_must_preserve_old_settings_and_new_rules(self):
        trusted = {'projects': {'prj_keep': []}}
        before = dict(self.project, trustedSources=trusted)
        lost = dict(self.project, trustedSources={'oidcProviders': self.rules})
        with self.assertRaisesRegex(deployment.DeploymentError, 'read-back differs'):
            self.configure([before, before, lost])

    def test_wrong_team_and_malformed_existing_rules_fail_closed(self):
        for project in (dict(self.project, accountId='team_other'),
                        dict(self.project, trustedSources=[]),
                        dict(self.project, trustedSources={'oidcProviders': []}),
                        dict(self.project, trustedSources={'oidcProviders': {access.ISSUER: {}}})):
            with self.subTest(project=project), patch.object(access.AccessAPI, 'request', return_value=project), \
                    patch.object(access.AccessAPI, 'update_trusted_sources') as put:
                with self.assertRaises(deployment.DeploymentError):
                    access.configure(self.output, rules_path=self.rules_path, environment=ENV)
                put.assert_not_called()
                self.output.unlink(missing_ok=True)

    def test_pr_tag_other_branch_and_missing_actions_identity_cannot_configure(self):
        for changes in ({'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_EVENT_NAME': 'pull_request_target'},
                        {'GITHUB_REF': 'refs/tags/images-v5-r2'}, {'GITHUB_REF': 'refs/heads/topic'},
                        {'GITHUB_ACTIONS': 'false'}):
            with self.subTest(changes=changes), patch.object(access, 'AccessAPI') as api:
                with self.assertRaises(deployment.DeploymentError):
                    access.configure(self.output, rules_path=self.rules_path, environment=dict(ENV, **changes))
                api.assert_not_called()

    def test_broader_rules_or_different_repository_are_rejected_before_api(self):
        changed = copy.deepcopy(self.rules)
        changed[access.ISSUER][0]['claims'].pop('ref')
        for rules in (changed, access.expected_rules('other/repo')):
            self.rules_path.write_text(json.dumps(rules))
            with patch.object(access, 'AccessAPI') as api, self.assertRaises(deployment.DeploymentError):
                access.configure(self.output, rules_path=self.rules_path, environment=ENV)
            api.assert_not_called()

    def test_patch_is_fixed_origin_field_and_team_with_no_secret_output(self):
        api = access.AccessAPI(ENV['VERCEL_TOKEN'], ENV['VERCEL_ORG_ID'])
        with patch.object(api.opener, 'open', return_value=io.BytesIO(b'{}')) as opened:
            api.update_trusted_sources('prj_example', {'oidcProviders': self.rules})
        request = opened.call_args.args[0]
        self.assertEqual(request.method, 'PATCH')
        self.assertEqual(request.full_url, 'https://api.vercel.com/v9/projects/prj_example?teamId=team_example')
        self.assertEqual(json.loads(request.data), {'trustedSources': {'oidcProviders': self.rules}})
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + ENV['VERCEL_TOKEN'])
        for error in (HTTPError('https://bad.test/private', 302, 'secret', {}, None), URLError('secret')):
            with patch.object(api.opener, 'open', side_effect=error):
                with self.assertRaises(deployment.DeploymentError) as raised:
                    api.update_trusted_sources('prj_example', {})
                self.assertNotIn('secret', str(raised.exception))

    def test_cli_does_not_print_arbitrary_exceptions(self):
        stream = io.StringIO()
        with patch.object(access, 'configure', side_effect=RuntimeError('private data')), redirect_stderr(stream):
            self.assertEqual(access.main(['--output', str(self.output)]), 1)
        self.assertNotIn('private data', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
