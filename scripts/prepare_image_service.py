#!/usr/bin/env python3
"""Build a minimal Vercel source directory without receiver secrets or state."""

import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    'image_protocol.py',
    'image_service/__init__.py',
    'image_service/__main__.py',
    'image_service/app.py',
    'image_service/render.py',
    'image_service/render_v2.py',
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
)


def prepare(destination):
    destination = Path(destination).absolute()
    # A fresh directory prevents accidental inclusion of old deployments or keys.
    destination.mkdir(parents=True, exist_ok=False)
    for name in FILES:
        source = ROOT / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f'Missing or linked source: {name}')
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
    args = parser.parse_args()
    print(prepare(args.destination))


if __name__ == '__main__':
    main()
