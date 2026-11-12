import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import check_vercel_release as check


class OriginTests(unittest.TestCase):
    def test_only_explicit_https_host_without_credentials_or_path(self):
        self.assertEqual(check.origin('https://images.example.test/', ['images.example.test']),
                         'images.example.test')
        for url in ('http://images.example.test', 'https://elsewhere.test',
                    'https://images.example.test@elsewhere.test',
                    'https://user:secret@images.example.test', 'https://images.example.test/image',
                    'https://images.example.test?secret=yes', 'https://images.example.test#token',
                    'https://images.example.test:8443', 'https://images.example.test:bad'):
            with self.subTest(url=url), self.assertRaises(check.VerificationError):
                check.origin(url, ['images.example.test'])

    def test_live_key_selection_rejects_public_fixture_and_ambiguity(self):
        protocol = Mock()
        protocol.load_keys.return_value = check.FIXTURE_KEYS
        with self.assertRaises(check.VerificationError):
            check.signing_key(protocol, {})
        protocol.load_keys.return_value = {'old': b'a' * 32, 'current': b'b' * 32}
        with self.assertRaises(check.VerificationError):
            check.signing_key(protocol, {})
        self.assertEqual(check.signing_key(protocol, {}, 'current'), ('current', b'b' * 32))


class TransportTests(unittest.TestCase):
    @staticmethod
    def connection(status=200, body=b'ok\n', headers=()):
        response = Mock(status=status)
        response.read.return_value = body
        response.getheaders.return_value = list(headers)
        connection = Mock()
        connection.getresponse.return_value = response
        return connection

    def test_tls_redirects_are_not_followed_and_secrets_never_enter_trace(self):
        connection = self.connection(302, b'private body', [('Location', 'https://attacker.test/secret'),
                                      ('Set-Cookie', 'credential=secret'), ('x-vercel-cache', 'MISS')])
        client = check.HTTPS('images.example.test', bypass='private-bypass-value')
        with patch.object(check.http.client, 'HTTPSConnection', return_value=connection) as factory:
            result = client.request('/v5/private-signed-path', label='sample')
        self.assertEqual(result['status'], 302)
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(factory.call_args.kwargs['context'].check_hostname)
        headers = connection.request.call_args.kwargs['headers']
        self.assertEqual(headers['x-vercel-protection-bypass'], 'private-bypass-value')
        recorded = json.dumps(client.trace)
        for value in ('private-signed-path', 'private-bypass-value', 'private body', 'attacker', 'credential'):
            self.assertNotIn(value, recorded)
        self.assertEqual(client.trace[0]['headers'], {'x-vercel-cache': 'MISS'})

    def test_network_errors_redact_original_exception(self):
        connection = self.connection()
        connection.request.side_effect = OSError('secret-key https://private-url')
        with patch.object(check.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaises(check.VerificationError) as error:
                check.HTTPS('images.example.test').request('/signed', label='sample')
        self.assertNotIn('secret-key', str(error.exception))
        self.assertNotIn('private-url', str(error.exception))
        connection.close.assert_called_once()

    def test_oidc_header_is_refreshed_per_request_and_never_recorded(self):
        connection = self.connection(headers=[('x-vercel-cache', 'HIT')])
        provider = Mock(side_effect=['private.oidc.first', 'private.oidc.second'])
        client = check.HTTPS('images.example.test', oidc=provider)
        with patch.object(check.http.client, 'HTTPSConnection', return_value=connection):
            client.request('/signed', label='first')
            client.request('/signed', label='second')
        self.assertEqual(provider.call_count, 2)
        headers = [call.kwargs['headers'] for call in connection.request.call_args_list]
        self.assertEqual(headers[0]['x-vercel-trusted-oidc-idp-token'], 'private.oidc.first')
        self.assertEqual(headers[1]['x-vercel-trusted-oidc-idp-token'], 'private.oidc.second')
        self.assertNotIn('private.oidc', json.dumps(client.trace))
        with self.assertRaises(check.VerificationError):
            check.HTTPS('images.example.test', bypass='secret', oidc=provider)
        with self.assertRaises(check.VerificationError):
            client.bootstrap_share('secret')

    def test_oversized_body_is_rejected(self):
        connection = self.connection(body=b'x' * 17)
        with patch.object(check, 'MAX_BODY', 16), \
                patch.object(check.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaises(check.VerificationError):
                check.HTTPS('images.example.test').request('/signed', label='sample')

    def test_share_bootstrap_uses_memory_cookie_without_recording_authentication(self):
        bootstrap = self.connection(307, headers=[('Location', '/healthz'),
                       ('Set-Cookie', '_vercel_jwt=private.jwt.cookie; Path=/; HttpOnly; Secure')])
        following = self.connection(headers=[('cache-control', 'no-store')])
        client = check.HTTPS('images.example.test')
        with patch.object(check.http.client, 'HTTPSConnection', side_effect=[bootstrap, following]) as factory:
            client.bootstrap_share('private-share+token')
            self.assertEqual(factory.call_count, 1)
            client.request('/healthz', label='health')
        self.assertEqual(bootstrap.request.call_args.args[1], '/healthz?_vercel_share=private-share%2Btoken')
        self.assertEqual(following.request.call_args.kwargs['headers']['Cookie'], '_vercel_jwt=private.jwt.cookie')
        self.assertEqual(client.trace[0], {'label': 'share-bootstrap', 'status': 307})
        recorded = json.dumps(client.trace)
        for value in ('private', '_vercel_share', '_vercel_jwt', 'Location', 'Set-Cookie'):
            self.assertNotIn(value, recorded)

    def test_share_bootstrap_rejects_cross_host_path_query_and_credential_redirects(self):
        invalid = ['https://attacker.test/healthz', '//images.example.test/healthz',
                   '/healthz?token=private', '/healthz#fragment', '/elsewhere',
                   'http://images.example.test/healthz',
                   'https://user:pass@images.example.test/healthz',
                   'https://images.example.test/healthz?token=private']
        for location in invalid:
            with self.subTest(location=location):
                connection = self.connection(302, headers=[('Location', location),
                                            ('Set-Cookie', '_vercel_jwt=private.jwt.cookie')])
                client = check.HTTPS('images.example.test')
                with patch.object(check.http.client, 'HTTPSConnection', return_value=connection) as factory:
                    with self.assertRaises(check.VerificationError):
                        client.bootstrap_share('private-token')
                self.assertEqual(factory.call_count, 1)
                self.assertIsNone(client.cookie)
                self.assertEqual(client.trace, [{'label': 'share-bootstrap', 'status': 302}])

    def test_share_bootstrap_rejects_missing_or_repeated_auth_cookie(self):
        for cookies in ([], [('Set-Cookie', 'unrelated=value')],
                        [('Set-Cookie', '_vercel_jwt=one'), ('Set-Cookie', '_vercel_jwt=two')]):
            with self.subTest(cookies=cookies):
                connection = self.connection(307, headers=[('Location', 'https://images.example.test/healthz'), *cookies])
                client = check.HTTPS('images.example.test')
                with patch.object(check.http.client, 'HTTPSConnection', return_value=connection):
                    with self.assertRaises(check.VerificationError):
                        client.bootstrap_share('private-token')
                self.assertIsNone(client.cookie)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.body = b'known fixed image bytes'
        digest = hashlib.sha256(self.body).hexdigest()
        self.item = {'label': 'v5/w1/nankai-advisory', 'mime': 'image/webp', 'encoding': 'w1',
                     'bytes': len(self.body), 'sha256': digest, 'etag': '"' + digest + '"',
                     'width': 1920, 'height': 1080}
        self.headers = {'content-type': 'image/webp', 'etag': self.item['etag'],
                        'x-content-type-options': 'nosniff',
                        'cache-control': 'immutable, public, max-age=31536000', 'x-vercel-cache': 'HIT'}

    def test_cdn_consumed_header_and_omitted_length_are_valid_but_bytes_must_match(self):
        response = {'status': 200, 'headers': self.headers, 'body': self.body}
        with patch.object(check, 'pixels', return_value=((1920, 1080), b'rgba')):
            check.image_response(response, self.item)
            with self.assertRaises(check.VerificationError):
                check.image_response(dict(response, body=b'changed'), self.item)

    def test_cached_head_and_minimal_304_while_testing_rejections(self):
        client = Mock()
        def request(path, *, label, **options):
            if label.endswith('/head'):
                return {'status': 200, 'headers': self.headers, 'body': b''}
            if label.endswith('/conditional'):
                return {'status': 304, 'headers': {'etag': self.item['etag']}, 'body': b''}
            if '/reject-' in label:
                status = 400 if label.endswith('query') else 405 if label.endswith('method') else 403
                return {'status': status, 'headers': {'cache-control': 'no-store'}, 'body': b'rejected\n'}
            return {'status': 200, 'headers': self.headers, 'body': self.body}
        client.request.side_effect = request
        with patch.object(check, 'pixels', return_value=((1920, 1080), b'rgba')):
            result = check.contract(client, '/v5/key/payload.signature.w1.webp', self.item)
        self.assertTrue(result['head'])
        self.assertTrue(result['conditional_get'])
        self.assertEqual(result['repeat_cache'], 'HIT')
        self.assertEqual(len(result['rejected']), 6)

    def test_acceptance_of_modified_signature_fails(self):
        client = Mock()
        def request(path, *, label, **options):
            if label.endswith('/head'):
                return {'status': 200, 'headers': self.headers, 'body': b''}
            if label.endswith('/conditional'):
                return {'status': 304, 'headers': {'etag': self.item['etag']}, 'body': b''}
            return {'status': 200, 'headers': self.headers, 'body': self.body}
        client.request.side_effect = request
        with patch.object(check, 'pixels', return_value=((1920, 1080), b'rgba')):
            with self.assertRaisesRegex(check.VerificationError, 'signature'):
                check.contract(client, '/v5/key/payload.signature.w1.webp', self.item)


class ExecutionTests(unittest.TestCase):
    def test_candidate_share_authentication_completes_checks_and_hit_aggregation(self):
        self.candidate_authentication('share')

    def test_candidate_oidc_authentication_completes_checks_and_hit_aggregation(self):
        self.candidate_authentication('oidc')

    def candidate_authentication(self, mode):
        corpus, bodies = [], {}
        for encoding, extension, mime, body in (('png', 'png', 'image/png', b'png'),
                                                ('w1', 'w1.webp', 'image/webp', b'webp')):
            path = '/v5/key/payload.' + encoding + 'sign.' + extension
            digest = hashlib.sha256(body).hexdigest()
            item = {'label': 'v5/' + encoding + '/nankai-advisory', 'version': 'v5',
                    'encoding': encoding, 'mime': mime, 'records': (('fixture', 1),),
                    'sha256': digest, 'etag': '"' + digest + '"', 'bytes': len(body),
                    'width': 1920, 'height': 1080}
            corpus.append(item)
            bodies[path] = item, body
        protocol = Mock()
        protocol.load_keys.return_value = {'key': b'k' * 32}
        protocol.make_path.side_effect = lambda records, selected, secret, version, encoding: next(
            path for path, (item, _) in bodies.items() if item['encoding'] == encoding)

        def connect(*args, **kwargs):
            connection = Mock()
            def response():
                method, target = connection.request.call_args.args[:2]
                headers = connection.request.call_args.kwargs['headers']
                if target.startswith('/healthz?'):
                    self.assertNotIn('Cookie', headers)
                    return TransportTests.connection(307, headers=[('Location', '/healthz'),
                          ('Set-Cookie', '_vercel_jwt=private.jwt.cookie; Secure; HttpOnly')]).getresponse()
                if mode == 'share':
                    self.assertEqual(headers['Cookie'], '_vercel_jwt=private.jwt.cookie')
                else:
                    self.assertNotIn('Cookie', headers)
                    self.assertEqual(headers['x-vercel-trusted-oidc-idp-token'], 'private.oidc.token')
                if target == '/healthz':
                    return TransportTests.connection(headers=[('Cache-Control', 'no-store')]).getresponse()
                if '?' in target or method == 'POST' or target not in bodies:
                    status = 400 if '?' in target else 405 if method == 'POST' else 403
                    return TransportTests.connection(status, b'rejected\n', [('Cache-Control', 'no-store')]).getresponse()
                item, body = bodies[target]
                if 'If-None-Match' in headers:
                    return TransportTests.connection(304, b'', [('ETag', item['etag'])]).getresponse()
                pairs = [('Content-Type', item['mime']), ('Content-Length', str(len(body))),
                         ('ETag', item['etag']), ('X-Content-Type-Options', 'nosniff'),
                         ('Cache-Control', 'public, max-age=31536000, immutable'), ('X-Vercel-Cache', 'HIT')]
                return TransportTests.connection(200, b'' if method == 'HEAD' else body, pairs).getresponse()
            connection.getresponse.side_effect = response
            return connection

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(check, 'verify_bundle', return_value={'manifest_sha256': 'candidate'}), \
                    patch.object(check, 'verify_public', return_value={'candidate_manifest_sha256': 'candidate', 'source_commit': 'source'}), \
                    patch.object(check, 'protocol_from_bundle') as runtime, \
                    patch.object(check, 'samples', return_value=corpus), \
                    patch.object(check.http.client, 'HTTPSConnection', side_effect=connect), \
                    patch.object(check, 'pixels', return_value=((1920, 1080), b'rgba')):
                runtime.return_value.__enter__.return_value = protocol
                report = check.check(root/'bundle', root/'public', 'https://images.example.test',
                                     ['images.example.test'], root/'result.json', phase='candidate',
                                     environment={'VERCEL_SHARE_TOKEN': 'private-share-token'} if mode == 'share' else {},
                                     oidc=Mock(return_value='private.oidc.token') if mode == 'oidc' else None)
            self.assertEqual(report['state'], 'success')
            if mode == 'share':
                self.assertEqual(report['requests'][0], {'label': 'share-bootstrap', 'status': 307})
            else:
                self.assertEqual(report['requests'][0]['label'], 'health')
                self.assertEqual(report['deployment_protection_mode'], 'oidc')
            self.assertGreater(report['checks']['cdn_hit_count'], 0)
            self.assertEqual(set(report['checks']['contracts']), {'png', 'w1'})
            recorded = (root/'result.json').read_text()
            self.assertNotIn('private-share-token', recorded)
            self.assertNotIn('private.jwt.cookie', recorded)
            self.assertNotIn('private.oidc.token', recorded)
            self.assertNotIn('/v5/key/', recorded)

    def test_before_and_production_ignore_preview_bypass(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for phase in ('before', 'production'):
                output = directory / (phase + '.json')
                with patch.object(check, 'verify_bundle', return_value={'manifest_sha256': 'a'}), \
                        patch.object(check, 'verify_public', return_value={'candidate_manifest_sha256': 'a', 'source_commit': 'b'}), \
                        patch.object(check, 'HTTPS') as client, \
                        patch.object(check, 'protocol_from_bundle', side_effect=ValueError('secret config')):
                    client.return_value.trace = []
                    with self.assertRaises(ValueError):
                        check.check(directory/'bundle', directory/'public', 'https://images.example.test',
                                    ['images.example.test'], output, phase=phase,
                                    environment={'VERCEL_AUTOMATION_BYPASS_SECRET': 'private-bypass',
                                                 'VERCEL_SHARE_TOKEN': 'private-share'}, oidc=Mock())
                self.assertIsNone(client.call_args.kwargs['bypass'])
                self.assertIsNone(client.call_args.kwargs['oidc'])
                client.return_value.bootstrap_share.assert_not_called()
                report = output.read_text()
                self.assertNotIn('secret config', report)
                self.assertNotIn('private-bypass', report)
                self.assertNotIn('private-share', report)


if __name__ == '__main__':
    unittest.main()
