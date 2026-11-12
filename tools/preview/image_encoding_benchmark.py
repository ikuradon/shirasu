"""Measure alternative encodings of an unchanged renderer PNG, offline.

This does not add a format to the image service. Each encode sample saves an
already decoded and prepared image; source PNG decoding and per-format mode
preparation are recorded separately. Target decode samples time Image.open and
load, without file I/O or conversion for the subsequent quality comparison.
"""

from io import BytesIO
import hashlib
import math
from pathlib import Path
from statistics import median
from time import perf_counter

from PIL import Image, ImageChops, features


MAX_REPEATS = 20
_SPECS = (
    ('png-original', 'PNG（現行）', 'PNG', 'image/png', 'png', {}),
    ('png-optimized', 'PNG（可逆・最適化）', 'PNG', 'image/png', 'png', {'optimize': True}),
    ('webp-lossless-m0', 'WebP（可逆・method 0）', 'WEBP', 'image/webp', 'webp',
     {'lossless': True, 'quality': 100, 'method': 0, 'exact': True}),
    ('webp-lossless', 'WebP（可逆・method 4）', 'WEBP', 'image/webp', 'webp',
     {'lossless': True, 'quality': 100, 'method': 4, 'exact': True}),
    ('webp-q90', 'WebP（非可逆・quality 90）', 'WEBP', 'image/webp', 'webp',
     {'lossless': False, 'quality': 90, 'method': 4}),
    ('jpeg-q90', 'JPEG（quality 90・4:4:4）', 'JPEG', 'image/jpeg', 'jpg',
     {'quality': 90, 'subsampling': 0, 'optimize': True}),
)
_AVIF = ('avif-q90', 'AVIF（quality 90・speed 8）', 'AVIF', 'image/avif', 'avif',
         {'quality': 90, 'speed': 8, 'subsampling': '4:4:4', 'max_threads': 1})


def _timings(samples):
    return {'samples': samples, 'median': median(samples)}


def _over_white(rgba):
    background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, rgba).convert('RGB')


def _quality(source_rgba, source_rgb, candidate):
    exact = source_rgba.tobytes() == candidate.tobytes()
    difference = ImageChops.difference(source_rgb, _over_white(candidate))
    histogram = difference.histogram()
    square_sum = sum((index % 256) ** 2 * count for index, count in enumerate(histogram))
    mse = square_sum / (source_rgba.width * source_rgba.height * 3)
    return {'pixel_exact': exact, 'rgb_mse': mse,
            'psnr_rgb_db': None if mse == 0 else 10 * math.log10(255 ** 2 / mse),
            'psnr_rgb_exact': mse == 0, 'comparison_background': 'white'}


def _decode_samples(data, repeats):
    elapsed = []
    candidate = None
    for _ in range(repeats):
        start = perf_counter()
        with Image.open(BytesIO(data)) as decoded:
            decoded.load()
            elapsed.append((perf_counter() - start) * 1000)
            if candidate is not None:
                candidate.close()
            candidate = decoded.convert('RGBA')
    return candidate, _timings(elapsed)


def evaluate(png, output_dir, *, repeats=3):
    """Save codec variants and return JSON-compatible measurements.

    ``output_dir`` may be a new or existing directory, but none of this run's
    filenames may already exist (including symlinks). A malformed source or
    invalid repeat count raises before output is created. An unavailable/failing
    alternative encoder produces an error row while other formats continue.

    Lossless candidates must match every decoded RGBA sample, including hidden
    RGB under transparent pixels. JPEG flattens transparency over white. PSNR
    compares all formats composited over white; null means infinite PSNR only
    when ``psnr_rgb_exact`` is true. Neither PSNR nor pixel equality is a claim
    about text readability or SNS-client support.
    """
    if type(repeats) is not int or not 1 <= repeats <= MAX_REPEATS:
        raise ValueError(f'repeats must be an integer from 1 to {MAX_REPEATS}')
    if not isinstance(png, bytes) or not png.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('An encoded PNG byte string is required')
    start = perf_counter()
    try:
        with Image.open(BytesIO(png)) as image:
            if image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('One static PNG is required')
            image.load()
            source = image.convert('RGBA')
    except (OSError, SyntaxError) as error:
        raise ValueError('Cannot decode source PNG') from error
    source_decode_ms = (perf_counter() - start) * 1000
    has_alpha = source.getchannel('A').getextrema() != (255, 255)
    source_rgb = _over_white(source)
    # Load plugin modules outside encode/decode timings. Codec-internal first
    # call costs remain visible in the first sample, followed by repeat samples.
    Image.init()
    specs = _SPECS + ((_AVIF,) if features.check('avif') else ())
    output = Path(output_dir)
    if output.is_symlink():
        raise FileExistsError('Output directory must not be a symlink')
    for name, _, _, _, extension, _ in specs:
        destination = output / f'{name}.{extension}'
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f'Encoding output already exists: {destination}')
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, label, format_name, mime, extension, parameters in specs:
        row = {'id': name, 'label': label, 'mime': mime, 'parameters': dict(parameters),
               'width': source.width, 'height': source.height, 'path': None,
               'source_decode_ms': source_decode_ms, 'alpha_flattened': False,
               'warnings': [], 'timing_scope': {
                   'source_decode_ms': 'PNG open/load and conversion to RGBA, once per input',
                   'prepare_ms': 'mode conversion from decoded RGBA, once per format',
                   'encode_ms': 'save prepared pixels to memory; excludes source decode and preparation',
                   'decode_ms': 'open/load target bytes from memory; excludes RGBA comparison conversion',
               }}
        prepared = None
        candidate = None
        try:
            start = perf_counter()
            if name == 'png-original':
                row['prepare_ms'] = 0.0
                row['encode_ms'] = None
                data = png
            else:
                if format_name == 'JPEG':
                    prepared = _over_white(source)
                    row['alpha_flattened'] = has_alpha
                    if has_alpha:
                        row['warnings'].append('透明部分を白背景に合成。透過は保持しません。')
                else:
                    # Dropping an all-opaque alpha channel changes no RGBA
                    # pixels and avoids paying for a redundant channel in PNG.
                    prepared = source.copy() if has_alpha else source.convert('RGB')
                row['prepare_ms'] = (perf_counter() - start) * 1000
                row['parameters']['mode'] = prepared.mode
                samples = []
                for _ in range(repeats):
                    buffer = BytesIO()
                    start = perf_counter()
                    prepared.save(buffer, format=format_name, **parameters)
                    samples.append((perf_counter() - start) * 1000)
                    data = buffer.getvalue()
                row['encode_ms'] = _timings(samples)
            candidate, row['decode_ms'] = _decode_samples(data, repeats)
            if candidate.size != source.size:
                raise ValueError('Encoded image dimensions changed')
            row.update(_quality(source, source_rgb, candidate))
            if (name in ('png-original', 'png-optimized', 'webp-lossless-m0', 'webp-lossless')
                    and not row['pixel_exact']):
                raise ValueError('Lossless encoding changed decoded RGBA pixels')
            row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                       ratio_to_png=len(data) / len(png))
        except Exception as error:
            row['error'] = f'{type(error).__name__}: {str(error)[:300]}'
            rows.append(row)
            continue
        finally:
            if prepared is not None:
                prepared.close()
            if candidate is not None:
                candidate.close()
        filename = f'{name}.{extension}'
        # A concurrent collision is still a fatal write failure, never an
        # encoder error that could hide accidental replacement of saved work.
        with (output / filename).open('xb') as target:
            target.write(data)
        row['path'] = filename
        rows.append(row)
    source.close()
    source_rgb.close()
    return rows
