"""Offline comparison of PNG, PNG-to-WebP, and direct Agg-to-WebP output.

The normal renderer and signed image service remain PNG-only. This experiment
temporarily redirects only the finishing figure's savefig call, under the same
lock as rendering, to Matplotlib's public WebP output. That backend encodes the
drawn RGBA buffer without first creating a PNG. The existing _finish still owns
text effects, DPI, face colour and all other presentation choices.

savefig_ms includes the final canvas draw and encoding; it is not an isolated
codec timing. Callers must measure the complete render separately. Transcode
stages are disjoint from savefig_ms and from one another.
"""

from dataclasses import dataclass
from io import BytesIO
from time import perf_counter
from types import MappingProxyType
from unittest.mock import patch

from PIL import Image


VARIANTS = ('png', 'png-webp', 'direct-webp')
WEBP_PARAMETERS = MappingProxyType({
    'lossless': True, 'quality': 100, 'method': 4, 'exact': True,
})


@dataclass(frozen=True)
class EncodedPreview:
    data: bytes
    mime: str
    category: str
    decision: object
    stages: dict


def transcode_png(png):
    """Return fixed lossless WebP bytes and PNG decode/prepare/encode timings.

    Opaque source pixels are converted to RGB, matching the earlier format
    benchmark. Otherwise all RGBA values, including hidden RGB at alpha zero,
    are retained. Pillow objects are closed before returning or propagating an
    encoder error. No input or output file is opened.
    """
    if not isinstance(png, bytes) or not png.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('An encoded PNG byte string is required')
    stages = {}
    started = perf_counter()
    with Image.open(BytesIO(png)) as decoded:
        if decoded.format != 'PNG' or getattr(decoded, 'n_frames', 1) != 1:
            raise ValueError('One static PNG is required')
        decoded.load()
        source = decoded.convert('RGBA')
    stages['transcode_decode_ms'] = (perf_counter() - started) * 1000
    with source:
        started = perf_counter()
        with source.getchannel('A') as alpha:
            has_alpha = alpha.getextrema() != (255, 255)
        prepared = source.copy() if has_alpha else source.convert('RGB')
        stages['transcode_prepare_ms'] = (perf_counter() - started) * 1000
        with prepared, BytesIO() as output:
            started = perf_counter()
            prepared.save(output, format='WEBP', **WEBP_PARAMETERS)
            stages['transcode_encode_ms'] = (perf_counter() - started) * 1000
            data = output.getvalue()
    return data, stages


def render(records, *, variant='png', cancel_reference=None):
    """Render one unchanged preview through the selected offline encoding path.

    The direct backend preserves savefig's existing DPI/facecolor arguments,
    replacing only PNG-specific format and metadata with the fixed WebP profile.
    Returning generic ``data`` avoids exposing WebP as a production PNG result.
    Neither service formats nor supported URL generations are registered here.
    """
    if variant not in VARIANTS:
        raise ValueError('Unknown offline encoding variant')
    from image_service import render_preview as preview

    stages = {'savefig_ms': 0.0, 'transcode_decode_ms': 0.0,
              'transcode_prepare_ms': 0.0, 'transcode_encode_ms': 0.0}
    # preview.render uses this RLock itself. Holding it before the temporary
    # module patch prevents another render from observing our experiment.
    with preview.base._LOCK:
        finish = preview._finish

        def measured_finish(fig, category, decision):
            savefig = fig.savefig

            def save(*args, **kwargs):
                if variant == 'direct-webp':
                    if kwargs.get('format') != 'png':
                        raise ValueError('The preview no longer finishes as PNG')
                    kwargs = dict(kwargs, format='webp', metadata=None,
                                  pil_kwargs=dict(WEBP_PARAMETERS))
                started = perf_counter()
                try:
                    return savefig(*args, **kwargs)
                finally:
                    stages['savefig_ms'] += (perf_counter() - started) * 1000

            with patch.object(fig, 'savefig', save):
                return finish(fig, category, decision)

        with patch.object(preview, '_finish', measured_finish):
            image = preview.render(records, cancel_reference=cancel_reference)
        data = image.png
        if variant == 'png-webp':
            data, transcode_stages = transcode_png(data)
            stages.update(transcode_stages)
        return EncodedPreview(data, 'image/png' if variant == 'png' else 'image/webp',
                              image.category, image.decision, stages)
