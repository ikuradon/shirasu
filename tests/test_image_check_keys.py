"""Deployment check keys must not replace or weaken the normal image keyring."""

import hashlib
import importlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from image_protocol import make_path


app_module = importlib.import_module('image_service.app')
PRIMARY = bytes(range(32))
CHECK = bytes(reversed(range(32)))
RECORDS = (('$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B',
            1794121200000),)
IMAGE = b'fixed renderer output'


class CheckKeyTests(unittest.TestCase):
    def request(self, path, *, primary=None, check=None, include_primary=True, include_check=False):
        settings = {}
        if include_primary:
            settings['SHIRASU_IMAGE_KEYS'] = primary if primary is not None else json.dumps({'k1': PRIMARY.hex()})
        if include_check:
            settings['SHIRASU_IMAGE_CHECK_KEYS'] = check
        response = {}

        def start(status, headers):
            response.update(status=status, headers=dict(headers))

        renderer = Mock()
        renderer.render.return_value = SimpleNamespace(png=IMAGE)
        with patch.dict('os.environ', settings, clear=True), \
                patch.object(app_module, 'import_module', return_value=renderer):
            response['body'] = b''.join(app_module.app({'PATH_INFO': path}, start))
        return response, renderer

    def assert_unconfigured(self, response):
        self.assertEqual(response['status'], '503 Service Unavailable')
        self.assertEqual(response['body'], b'image service is not configured\n')
        self.assertEqual(response['headers']['Cache-Control'], 'no-store')
        self.assertNotIn('ETag', response['headers'])

    def test_optional_check_key_authenticates_without_changing_primary_response(self):
        primary_path = make_path(RECORDS, 'k1', PRIMARY, version='v2')
        check_path = make_path(RECORDS, 'check-run-123', CHECK, version='v2')
        config = json.dumps({'check-run-123': CHECK.hex()})
        normal, _ = self.request(primary_path)
        with_check, _ = self.request(primary_path, check=config, include_check=True)
        checked, renderer = self.request(check_path, check=config, include_check=True)
        self.assertEqual(normal['status'], '200 OK')
        self.assertEqual(with_check, normal)
        self.assertEqual(checked, normal)
        self.assertEqual(checked['headers']['ETag'], '"' + hashlib.sha256(IMAGE).hexdigest() + '"')
        renderer.render.assert_called_once_with(RECORDS)

    def test_check_url_is_not_accepted_without_its_deployment_key(self):
        path = make_path(RECORDS, 'check-run-123', CHECK, version='v2')
        response, renderer = self.request(path)
        self.assertEqual(response['status'], '403 Forbidden')
        renderer.render.assert_not_called()

    def test_primary_keyring_is_still_mandatory(self):
        config = json.dumps({'check-run-123': CHECK.hex()})
        for options in ({'include_primary': False}, {'primary': ''}, {'primary': '{}'}):
            with self.subTest(options=options):
                response, renderer = self.request('/healthz', check=config, include_check=True, **options)
                self.assert_unconfigured(response)
                renderer.verify_assets.assert_not_called()

    def test_malformed_check_configuration_fails_closed_even_for_primary_urls(self):
        path = make_path(RECORDS, 'k1', PRIMARY, version='v2')
        invalid = ['', '{}', '[]', 'null', 'not-json',
                   json.dumps({'check-run-123': 'short-secret'}),
                   json.dumps({'check-run-123': 123}),
                   json.dumps({'k2': CHECK.hex()}),
                   json.dumps({'Check-run-123': CHECK.hex()}),
                   json.dumps({'check-' + 'x' * 40: CHECK.hex()}),
                   '{"check-dup":"' + CHECK.hex() + '","check-dup":"' + PRIMARY.hex() + '"}']
        for value in invalid:
            with self.subTest(value=value):
                for target in (path, '/healthz'):
                    response, renderer = self.request(target, check=value, include_check=True)
                    self.assert_unconfigured(response)
                    renderer.render.assert_not_called()
                    renderer.verify_assets.assert_not_called()

    def test_existing_primary_key_ids_cannot_be_replaced_even_by_same_value(self):
        primary = json.dumps({'check-existing': PRIMARY.hex()})
        path = make_path(RECORDS, 'check-existing', PRIMARY, version='v2')
        unchanged, _ = self.request(path, primary=primary)
        self.assertEqual(unchanged['status'], '200 OK')
        for secret in (PRIMARY, CHECK):
            with self.subTest(same_value=secret == PRIMARY):
                response, renderer = self.request(path, primary=primary,
                    check=json.dumps({'check-existing': secret.hex()}), include_check=True)
                self.assert_unconfigured(response)
                renderer.render.assert_not_called()

    def test_check_key_cannot_sign_for_a_primary_id(self):
        path = make_path(RECORDS, 'k1', CHECK, version='v2')
        response, renderer = self.request(path,
            check=json.dumps({'check-run-123': CHECK.hex()}), include_check=True)
        self.assertEqual(response['status'], '403 Forbidden')
        renderer.render.assert_not_called()

    def test_valid_check_configuration_keeps_health_contract(self):
        normal, _ = self.request('/healthz')
        checked, _ = self.request('/healthz',
            check=json.dumps({'check-run-123': CHECK.hex()}), include_check=True)
        self.assertEqual(normal['status'], '200 OK')
        self.assertEqual(checked, normal)


if __name__ == '__main__':
    unittest.main()
