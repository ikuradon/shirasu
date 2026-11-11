"""Bounded reuse of static geography in unpublished renderers only.

Cache geometry content, never report state or artists. Frozen render helpers
remain the authority on projection, validation and polygon winding on misses.
"""

from collections import OrderedDict
import hashlib
import json
import math
import struct
import sys
from threading import RLock

import numpy as np
from matplotlib.colors import to_rgba
from matplotlib.lines import Line2D
from matplotlib.patches import PathPatch
from matplotlib.path import Path

from image_service import region_map as original


def _plain_json(value):
    """Only exact JSON types have unambiguous serialization/validation here.

    json.dumps accepts tuples and numeric subclasses, but the frozen geometry
    validator treats them differently in some positions. Bypass their cache.
    """
    kind = type(value)
    if kind is dict:
        return all(type(key) is str and _plain_json(part) for key, part in value.items())
    if kind is list:
        return all(_plain_json(part) for part in value)
    return kind in (str, int, float, bool, type(None))


def _freeze(value):
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        # Immutable backing prevents even setflags(write=True), unlike an
        # owning ndarray whose WRITEABLE flag was merely cleared.
        return np.ndarray(value.shape, dtype=value.dtype, buffer=value.tobytes(order='C'))
    return tuple(_freeze(part) for part in value)


def _retained_size(value, seen=None):
    """Estimate retained storage including the many small ring/tuple headers."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, np.ndarray) and value.base is not None:
        size += _retained_size(value.base, seen)
    elif isinstance(value, (tuple, list)):
        size += sum(_retained_size(part, seen) for part in value)
    return size


class GeometryCache:
    """One LRU for projected rings and compiled Path buffers.

    The byte budget estimates keys, tuple/array headers and backing buffers,
    with an allowance for each LRU entry. It is not a process RSS limit: source
    catalogues, active figures and temporary build allocations are separate.
    """

    def __init__(self, *, max_entries=256, max_bytes=32 * 1024 * 1024):
        if any(type(value) is not int or value < 0 for value in (max_entries, max_bytes)):
            raise ValueError('Geometry cache limits must be nonnegative integers')
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._lock = RLock()
        self.clear()

    def clear(self):
        with self._lock:
            self._items = OrderedDict()
            self._bytes = 0
            self._hits = self._misses = self._evictions = self._skipped = 0

    def info(self):
        with self._lock:
            return dict(entries=len(self._items), retained_bytes=self._bytes,
                        max_entries=self.max_entries, max_bytes=self.max_bytes,
                        hits=self._hits, misses=self._misses,
                        evictions=self._evictions, skipped=self._skipped)

    def _get(self, key, build):
        with self._lock:
            if key is not None and key in self._items:
                self._hits += 1
                self._items.move_to_end(key)
                return self._items[key][0]
            self._misses += 1
            # Exceptions are never retained. Geometry-only callers do not
            # compile Paths, preserving their original validation sequence.
            value = build()
            size = _retained_size((key, value)) + 256
            if key is None or not self.max_entries or size > self.max_bytes:
                self._skipped += 1
                return value
            while self._items and (len(self._items) >= self.max_entries
                                   or self._bytes + size > self.max_bytes):
                _, (_, removed) = self._items.popitem(last=False)
                self._bytes -= removed
                self._evictions += 1
            self._items[key] = value, size
            self._bytes += size
            return value

    def geometry(self, feature, layer, reference):
        key = None
        if (isinstance(layer, str) and type(reference) in (int, float)
                and math.isfinite(reference)):
            try:
                source = feature.get('geometry')
                if _plain_json(source):
                    raw = json.dumps(source, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':'), allow_nan=False).encode('ascii')
                    key = ('geometry', layer, reference, hashlib.sha256(raw).digest())
            except (AttributeError, TypeError, ValueError, OverflowError, RecursionError):
                # Malformed input must still reach the original validator,
                # including its established exception and fallback reason.
                pass
        return self._get(key, lambda: _freeze(original._geometry(feature, layer, reference)))

    def compound_path(self, polygons):
        polygons = tuple(polygons)
        digest = hashlib.sha256()
        try:
            # Include structure as well as coordinates: changing a hole into
            # an exterior must not hit the same compiled winding geometry.
            digest.update(struct.pack('>Q', len(polygons)))
            for polygon in polygons:
                digest.update(struct.pack('>Q', len(polygon)))
                for ring in polygon:
                    if (type(ring) is not np.ndarray or ring.ndim != 2
                            or ring.shape[1] != 2 or ring.dtype != np.dtype('float64')):
                        raise TypeError('Nonstandard polygon array')
                    digest.update(struct.pack('>Q', len(ring)))
                    digest.update(ring.tobytes(order='C'))
            key = ('path', digest.digest())
        except (TypeError, ValueError, OverflowError, struct.error):
            key = None

        def build():
            path = Path.make_compound_path(*(original._polygon_path(polygon) for polygon in polygons))
            return _freeze(path.vertices), _freeze(path.codes)

        vertices, codes = self._get(key, build)
        # Path flags and artist state belong to the current render. Only the
        # expensive compiled buffers are shared, and they cannot be modified.
        return Path(vertices, codes, readonly=True)


_CACHE = GeometryCache()


def geometry(feature, layer, reference):
    return _CACHE.geometry(feature, layer, reference)


def compound_path(polygons):
    return _CACHE.compound_path(polygons)


def paint_regions(ax, leaves, layer):
    """Match the frozen regional painter; reuse only static coordinate data."""
    for code, leaf in sorted(leaves.items()):
        polygons = []
        for _, parts in leaf['parts']:
            if layer == 'tsunami':
                for line in parts:
                    ax.add_line(Line2D(line[:, 0], line[:, 1], color=leaf['color'], linewidth=4.6,
                                      solid_capstyle='round', zorder=4, gid=f'region-coast-{code}'))
            else:
                polygons.extend(parts)
        if polygons:
            path = compound_path(polygons)
            face = to_rgba(leaf['color'], .18) if layer == 'marine' else to_rgba('#a3b3c2', .035)
            edge = leaf['color'] if layer == 'marine' else '#8b98a5'
            ax.add_patch(PathPatch(path, facecolor=face,
                                   edgecolor=edge, linewidth=1.25, zorder=3, gid=f'region-area-{code}'))
