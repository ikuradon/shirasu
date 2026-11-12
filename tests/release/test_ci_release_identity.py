"""Packaging revisions cannot change a renderer identity or redirect a tag."""

import copy
import unittest

from tools.release import release_identity as identity


class ReleaseIdentityTests(unittest.TestCase):
    def test_default_distribution_revision_keeps_v5_renderer(self):
        self.assertEqual(identity.DEFAULT_RELEASE_TAG, 'images-v5-r1')
        self.assertEqual(identity.validate_release_tag(identity.DEFAULT_RELEASE_TAG, 'v5'),
                         'images-v5-r1')

    def test_base_and_revision_tags_are_fixed_canonical_names(self):
        for tag, version in (('images-v1', 'v1'), ('images-v5', 'v5'),
                             ('images-v5-r1', 'v5'), ('images-v12-r37', 'v12')):
            with self.subTest(tag=tag):
                self.assertEqual(identity.validate_release_tag(tag, version), tag)

    def test_revision_never_relabels_another_renderer_version(self):
        for tag in ('images-v4', 'images-v4-r1', 'images-v50-r1'):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, 'candidate version'):
                identity.validate_release_tag(tag, 'v5')

    def test_unsafe_mutable_and_noncanonical_tags_are_rejected(self):
        tags = (None, 5, True, '', 'latest', 'main', 'v5', 'images-v0',
                'images-v05', 'images-v5-r0', 'images-v5-r01', 'images-v5-r',
                'images-v5-R1', 'images-v5-r1-r2', 'images-v5/extra',
                '../images-v5', 'images-v5;command', 'images-v5\n',
                ' images-v5', 'images-v5 ', 'images-v５', 'images-v5-r١',
                'images-v5-r' + '1' * 120)
        for tag in tags:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                identity.validate_release_tag(tag, 'v5')

    def test_renderer_version_is_itself_validated(self):
        for version in (None, True, 5, '', '5', 'v0', 'v05', 'v5-r1', 'v5\n', 'v５'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                identity.validate_release_tag('images-v5', version)

    def test_schema_1_recovers_its_implicit_base_tag_without_mutation(self):
        receipt = {'schema_version': 1, 'version': 'v5', 'source_commit': 'a' * 40}
        before = copy.deepcopy(receipt)
        self.assertEqual(identity.receipt_tag(receipt), 'images-v5')
        self.assertEqual(receipt, before)

    def test_schema_1_rejects_even_matching_or_null_explicit_tag(self):
        for tag in (None, 'images-v5', 'images-v5-r1'):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, 'Schema 1'):
                identity.receipt_tag({'schema_version': 1, 'version': 'v5', 'release_tag': tag})

    def test_schema_2_reads_explicit_revision_without_mutation(self):
        receipt = {'schema_version': 2, 'version': 'v5', 'release_tag': 'images-v5-r1'}
        before = copy.deepcopy(receipt)
        self.assertEqual(identity.receipt_tag(receipt), 'images-v5-r1')
        self.assertEqual(receipt, before)
        self.assertEqual(identity.receipt_tag(dict(receipt, release_tag='images-v5')), 'images-v5')

    def test_schema_2_does_not_guess_a_missing_or_mismatched_tag(self):
        for fields in ({}, {'release_tag': None}, {'release_tag': 'images-v6-r1'},
                       {'release_tag': '../../other'}, {'release_tag': 'latest'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                identity.receipt_tag({'schema_version': 2, 'version': 'v5', **fields})

    def test_receipt_schema_must_be_an_integer_supported_version(self):
        for receipt in (None, [], 'v5', {}, {'schema_version': True, 'version': 'v5'},
                        {'schema_version': False, 'version': 'v5'},
                        {'schema_version': 1.0, 'version': 'v5'},
                        {'schema_version': '2', 'version': 'v5'},
                        {'schema_version': 0, 'version': 'v5'},
                        {'schema_version': 3, 'version': 'v5'}):
            with self.subTest(receipt=receipt), self.assertRaisesRegex(ValueError, 'Unsupported'):
                identity.receipt_tag(receipt)

    def test_both_schemas_require_a_canonical_renderer_version(self):
        for schema in (1, 2):
            for version in (None, True, 5, 'v0', 'v05', 'v5-r1'):
                fields = {'schema_version': schema, 'version': version}
                if schema == 2:
                    fields['release_tag'] = 'images-v5-r1'
                with self.subTest(schema=schema, version=version), self.assertRaises(ValueError):
                    identity.receipt_tag(fields)


if __name__ == '__main__':
    unittest.main()
