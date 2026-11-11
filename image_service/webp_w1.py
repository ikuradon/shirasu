"""Frozen lossless encoding profile; changing its bytes needs a new profile."""

from io import BytesIO
from types import MappingProxyType

import PIL
from PIL import features


PARAMETERS = MappingProxyType({'lossless': True, 'quality': 100, 'method': 4, 'exact': True})
PILLOW_VERSION = '13.0.0'
WEBP_VERSION = '1.6.0'


def verify_encoder():
    # Refuse a silently changed encoder behind an immutable published URL.
    # The package lock pins Pillow; this also checks the native codec it loads.
    if (PIL.__version__ != PILLOW_VERSION or not features.check('webp')
            or features.version('webp') != WEBP_VERSION):
        raise RuntimeError('The frozen w1 WebP encoder is unavailable')
    return True


def encode(fig, *, dpi, facecolor):
    verify_encoder()
    with BytesIO() as output:
        fig.savefig(output, format='webp', dpi=dpi, facecolor=facecolor,
                    metadata=None, pil_kwargs=dict(PARAMETERS))
        return output.getvalue()
