"""Separate immutable distribution tags from the image URL's render version."""

import re


DEFAULT_RELEASE_TAG = 'images-v5-r1'
RELEASE_PLATFORM = 'linux/arm64'
RELEASE_TAG_RE = re.compile(r'images-(?P<version>v[1-9][0-9]*)(?:-r[1-9][0-9]*)?')
VERSION_RE = re.compile(r'v[1-9][0-9]*')


def validate_release_tag(tag, version):
    """Validate one fixed distribution tag and its unchanged render version."""
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise ValueError('Invalid renderer version for release tag')
    if not isinstance(tag, str) or len(tag) > 128:
        raise ValueError('Invalid release tag')
    match = RELEASE_TAG_RE.fullmatch(tag)
    if match is None:
        raise ValueError('Invalid release tag')
    if match['version'] != version:
        raise ValueError('Release tag differs from the candidate version')
    return tag


def receipt_tag(receipt):
    """Read schema 1's implicit base tag or schema 2's explicit revision tag.

    Schema 1 never defined release_tag. Reject that field rather than letting
    old and new readers assign different tags to the same receipt. This helper
    validates tag identity only; the caller validates hashes and other fields.
    """
    if (not isinstance(receipt, dict)
            or type(receipt.get('schema_version')) is not int
            or receipt['schema_version'] not in (1, 2)):
        raise ValueError('Unsupported release receipt')
    version = receipt.get('version')
    if receipt['schema_version'] == 1:
        if 'release_tag' in receipt:
            raise ValueError('Schema 1 receipt must not contain release_tag')
        if not isinstance(version, str):
            raise ValueError('Invalid renderer version for release tag')
        tag = 'images-' + version
    else:
        tag = receipt.get('release_tag')
    return validate_release_tag(tag, version)
