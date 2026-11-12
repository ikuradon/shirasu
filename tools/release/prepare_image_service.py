#!/usr/bin/env python3
"""Build a minimal Vercel source directory without receiver secrets or state."""

import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    'shirasu/__init__.py',
    'shirasu/images/__init__.py',
    'licenses/pyserial-3.5-LICENSE.txt',
    'shirasu/images/versions.py',
    'shirasu/images/protocol.py',
    'image_service/__init__.py',
    'image_service/__main__.py',
    'image_service/app.py',
    'image_service/render.py',
    'image_service/render_v2.py',
    'image_service/render_v3.py',
    'image_service/render_v4.py',
    'image_service/weather_map_v4.py',
    'image_service/map_v4.py',
    'image_service/map_v3.py',
    'image_service/map_base_v3.py',
    'image_service/weather_map.py',
    'image_service/eew_map.py',
    'image_service/volcano_map.py',
    'image_service/region_map.py',
    'image_service/regional_reports.py',
    'image_service/marine_regions.py',
    'image_service/assets/v1/manifest.json',
    'image_service/assets/v1/land.geojson',
    'image_service/assets/v1/NotoSansCJKjp-Regular.otf',
    'image_service/assets/v1/NotoSansCJKjp-Bold.otf',
    'image_service/assets/v1/OFL.txt',
    'image_service/assets/v1/FONT_NOTICE.txt',
    'image_service/assets/v1/NATURAL_EARTH.txt',
    'image_service/assets/v2/manifest.json',
    'image_service/assets/v2/JMA_NOTICE.txt',
    'image_service/assets/v2/marine.geojson',
    'image_service/assets/v2/tsunami.geojson',
    'image_service/assets/v2/prefectures.geojson',
    'image_service/assets/v2/groups.json',
    'image_service/assets/v3/regional_manifest.json',
    'image_service/assets/v3/land.geojson.zlib',
    'image_service/assets/v3/land_manifest.json',
    'image_service/assets/v3/LAND_NOTICE.txt',
    'image_service/assets/v3/REGIONAL_NOTICE.txt',
    'image_service/assets/v3/weather.geojson.zlib',
    'image_service/assets/v3/eew.geojson.zlib',
    'image_service/assets/v3/weather_groups.json',
    'image_service/assets/v3/eew_groups.json',
    'image_service/assets/v3/volcano.json',
    'image_service/assets/v3/volcano_manifest.json',
    'image_service/assets/v3/VOLCANO_NOTICE.txt',
)

PREVIEW_FILES = (
    'shirasu/reports/__init__.py',
    'shirasu/reports/dcx.py',
    'shirasu/images/nankai.py',
    'image_service/render_preview.py',
    'image_service/webp_w1.py',
    'image_service/preview_nankai.py',
    'image_service/preview_cancellation.py',
    'image_service/preview_cancel_scope.py',
    'image_service/preview_cancel_map.py',
    'image_service/preview_cancel_card.py',
    'image_service/preview_wide_layout.py',
    'image_service/preview_wide_regions.py',
    'image_service/preview_wide_points.py',
    'image_service/preview_wide_special.py',
    'image_service/preview_wide_tsunami.py',
    'image_service/preview_wide_flood.py',
    'image_service/preview_scale.py',
    'image_service/preview_place_labels.py',
    'image_service/preview_wide_dcx.py',
    'image_service/preview_dcx_reports.py',
    'image_service/preview_dcx_geometry.py',
    'image_service/preview_dcx_areas.py',
    'image_service/assets/preview/dcx_wards.geojson.zlib',
    'image_service/assets/preview/dcx_wards_manifest.json',
    'image_service/assets/preview/DCX_WARDS_NOTICE.txt',
    'image_service/preview_diagnostics.py',
    'image_service/preview_diagnostic_errors.py',
    'image_service/nwp_map.py',
    'image_service/nwp_reports.py',
    'image_service/assets/preview/nwp_points.json',
    'image_service/assets/preview/nwp_manifest.json',
    'image_service/assets/preview/NWP_NOTICE.txt',
    'image_service/ash_map.py',
    'image_service/ash_reports.py',
    'image_service/preview_labels.py',
    'image_service/assets/preview/municipalities.geojson.zlib',
    'image_service/assets/preview/municipalities_manifest.json',
    'image_service/assets/preview/MUNICIPALITIES_NOTICE.txt',
    'image_service/flood_map.py',
    'image_service/flood_reports.py',
    'image_service/preview_context.py',
    'image_service/preview_regions.py',
    'image_service/preview_geometry_cache.py',
    'image_service/preview_points.py',
    'image_service/assets/preview/prefecture_boundaries.geojson.zlib',
    'image_service/assets/preview/prefecture_boundaries_manifest.json',
    'image_service/assets/preview/PREFECTURE_BOUNDARIES_NOTICE.txt',
    'image_service/assets/preview/flood.geojson.zlib',
    'image_service/assets/preview/flood_manifest.json',
    'image_service/assets/preview/FLOOD_NOTICE.txt',
)


def prepare(destination, *, include_preview=False):
    destination = Path(destination).absolute()
    names = FILES + (PREVIEW_FILES if include_preview else ())
    # Check materialized assets before creating a partial output directory.
    for name in names:
        source = ROOT / name
        if source.is_symlink() or not source.is_file():
            if (not source.is_symlink() and not source.exists()
                    and name.startswith('image_service/assets/')
                    and name != 'image_service/assets/v1/manifest.json'):
                raise ValueError('Prepare vendor assets first: python3 -B tools/assets/prepare_vendor_assets.py'
                                 f' (missing: {name})')
            raise ValueError(f'Missing or linked source: {name}')
    # A fresh directory prevents accidental inclusion of old deployments or keys.
    destination.mkdir(parents=True, exist_ok=False)
    for name in names:
        source = ROOT / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(ROOT / 'image_service/requirements.txt', destination / 'requirements.txt')
    (destination / 'api').mkdir()
    (destination / 'api/image.py').write_text('from image_service.app import app\n')
    (destination / '.python-version').write_text('3.12\n')
    (destination / 'vercel.json').write_text(json.dumps({
        '$schema': 'https://openapi.vercel.sh/vercel.json',
        # Leave room for a nationwide regional map to fall back to its summary
        # after bounded label placement, including a cold Python process.
        'functions': {'api/image.py': {'maxDuration': 60}},
        'rewrites': [{'source': '/(.*)', 'destination': '/api/image'}],
    }, indent=2) + '\n')
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--include-preview', action='store_true',
                        help='include unpublished sources for local builds; not for deployment')
    args = parser.parse_args()
    print(prepare(args.destination, include_preview=args.include_preview))


if __name__ == '__main__':
    main()
