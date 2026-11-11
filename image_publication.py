"""Lightweight image URL creation; the receiver never renders or fetches images."""

from dataclasses import dataclass, field
from datetime import datetime
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit

from image_protocol import VERSION, load_keys, make_path, validate_encoding
from image_versions import NANKAI_TOPIC_VERSIONS


@dataclass(frozen=True)
class ImagePublisher:
    base_url: str
    key_id: str
    secret: bytes = field(repr=False)
    version: str = VERSION
    encoding: str = 'png'

    def __post_init__(self):
        error = 'SHIRASU_IMAGE_BASE_URL must be an HTTPS origin without credentials, path or query'
        if not isinstance(self.base_url, str):
            raise ValueError(error)
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port  # Access validates nonnumeric/out-of-range ports.
        except ValueError as cause:
            raise ValueError(error) from cause
        if (parsed.scheme != 'https' or not parsed.netloc or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or any(c in self.base_url for c in ('?', '#', '\\', '%'))
                or not self.base_url.isascii()
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.base_url)
                or port == 0 or parsed.netloc.endswith(':')):
            raise ValueError(error)
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            labels = parsed.hostname.rstrip('.').split('.')
            if len(parsed.hostname) > 253 or not all(
                    re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                    for label in labels):
                raise ValueError(error)
        # Validate the key through the shared protocol, without producing a URL.
        if not isinstance(self.key_id, str):
            raise ValueError('Image signing key ID must be a string')
        if not isinstance(self.secret, bytes):
            raise ValueError('Image signing key must be bytes')
        load_keys(json.dumps({self.key_id: self.secret.hex()}))
        validate_encoding(self.encoding, self.version)
        object.__setattr__(self, 'base_url', self.base_url.rstrip('/'))

    @classmethod
    def from_environment(cls, environment):
        values = [environment.get(name, '') for name in (
            'SHIRASU_IMAGE_BASE_URL', 'SHIRASU_IMAGE_KEY_ID', 'SHIRASU_IMAGE_SIGNING_KEY',
        )]
        if not any(values):
            return None
        if not all(values):
            raise ValueError('Set all three SHIRASU_IMAGE_BASE_URL, SHIRASU_IMAGE_KEY_ID and SHIRASU_IMAGE_SIGNING_KEY')
        base, key_id, key_hex = values
        keys = load_keys(json.dumps({key_id: key_hex}))
        return cls(base, key_id, keys[key_id],
                   version=environment.get('SHIRASU_IMAGE_VERSION', VERSION),
                   encoding=environment.get('SHIRASU_IMAGE_ENCODING', 'png'))

    def url(self, entries):
        return self.base_url + make_path(entries, self.key_id, self.secret,
                                        version=self.version, encoding=self.encoding)

    @property
    def mime(self):
        return 'image/webp' if self.encoding == 'w1' else 'image/png'

    @property
    def supports_nankai_topic(self):
        return self.version in NANKAI_TOPIC_VERSIONS


def image_entry(report):
    """Retain the original decoder's date context, not the publication time."""
    timestamp = report.timestamp
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        raise ValueError('Image reports require a timezone-aware receipt time')
    seconds = timestamp.timestamp()
    if not math.isfinite(seconds):
        raise ValueError('Invalid image receipt time')
    return report.nmea, round(seconds * 1000)


def with_image(message, image_url):
    """Keep the NMEA records at the end for saved-event inspection."""
    body, separator, original = message.partition('\n---\n')
    return body + '\n\n' + image_url + (separator + original if separator else '')
