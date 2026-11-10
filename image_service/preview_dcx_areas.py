"""Exact DCX municipality lookup, with independent supplemental ward geometry."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import zlib

from image_service import ash_map

ASSETS = Path(__file__).parent / 'assets' / 'preview'
MAX_BYTES = 8 * 1024 * 1024


@lru_cache(maxsize=1)
def verify_assets():
    manifest = json.loads((ASSETS / 'dcx_wards_manifest.json').read_text())
    for name in ('dcx_wards.geojson.zlib', 'DCX_WARDS_NOTICE.txt'):
        raw = (ASSETS / name).read_bytes()
        if len(raw) != manifest[name]['bytes'] or hashlib.sha256(raw).hexdigest() != manifest[name]['sha256']:
            raise RuntimeError('DCX ward assets do not match their reviewed source')
    return True


@lru_cache(maxsize=1)
def _wards():
    verify_assets()
    try:
        return _load_wards()
    except (ValueError, KeyError, TypeError, zlib.error) as error:
        # Asset failures must not look like an unsupported received area.
        raise RuntimeError('Invalid DCX ward asset structure') from error


def _load_wards():
    decoder = zlib.decompressobj()
    raw = decoder.decompress((ASSETS / 'dcx_wards.geojson.zlib').read_bytes(), MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise RuntimeError('Invalid compressed DCX ward geometry')
    collection = json.loads(raw)
    if collection.get('type') != 'FeatureCollection':
        raise RuntimeError('Invalid DCX ward feature collection')
    features = {}
    for feature in collection['features']:
        code = int(feature['properties']['code'])
        if (code in features or feature.get('type') != 'Feature'
                or feature['geometry']['type'] not in ('Polygon', 'MultiPolygon')):
            raise RuntimeError('Duplicate or invalid DCX ward feature')
        features[code] = feature
    manifest = json.loads((ASSETS / 'dcx_wards_manifest.json').read_text())['dcx_wards.geojson.zlib']
    if (len(features) != 171 or set(features) != set(manifest['codes'])
            or len(raw) != manifest['uncompressed_bytes']
            or not set(features).isdisjoint(ash_map._municipalities())):
        raise RuntimeError('Invalid or overlapping DCX ward coverage')
    return features


def municipality(code):
    """No parent lookup or name matching, including identically named old wards."""
    feature = ash_map._municipalities().get(code)
    return feature if feature is not None else _wards().get(code)
