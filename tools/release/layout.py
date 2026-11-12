"""Explicit source paths and entrypoints for current and retained releases."""

# Public artifacts before the package migration used flat modules. Keep this
# mapping only in release tooling: the application has no import compatibility
# shims, and retained artifacts are never rewritten.
MODULES = {
    'main': 'shirasu.service',
    'receiver': 'shirasu.receiver',
    'outbox': 'shirasu.outbox',
    'inspection': 'shirasu.inspection',
    'preview': 'shirasu.preview',
    'backup': 'shirasu.backup',
    'backup_worker': 'shirasu.backup_worker',
    'metrics': 'shirasu.metrics',
    'image_protocol': 'shirasu.images.protocol',
    'image_versions': 'shirasu.images.versions',
    'image_publication': 'shirasu.images.publication',
    'image_alt': 'shirasu.images.alt',
    'nankai_image': 'shirasu.images.nankai',
    'message_format': 'shirasu.reports.format',
    'dcx_instructions': 'shirasu.reports.dcx',
    'typhoon': 'shirasu.reports.typhoon',
    'nankai': 'shirasu.reports.nankai',
}
MOVED_FILES = {name + '.py': module.replace('.', '/') + '.py'
               for name, module in MODULES.items()}
PACKAGE_INITS = ('shirasu/__init__.py', 'shirasu/images/__init__.py',
                 'shirasu/reports/__init__.py')
PROFILE_PATH = 'shirasu/images/versions.py'
LEGACY_RECEIVER_FILES = (
    'main.py', 'receiver.py', 'outbox.py', 'inspection.py', 'preview.py',
    'message_format.py', 'dcx_instructions.py', 'typhoon.py', 'nankai.py',
    'image_versions.py', 'image_protocol.py', 'image_publication.py',
    'image_alt.py', 'nankai_image.py', 'backup.py', 'backup_worker.py', 'metrics.py',
    'requirements.txt', 'requirements-test.txt', 'Dockerfile', '.dockerignore',
    'licenses/pyserial-3.5-LICENSE.txt',
)


# Frozen paths from the retained flat v5 release. New renderer additions must
# not change which files can be restored from an already published artifact.
LEGACY_IMAGE_FILES = (
    '.python-version',
    'Dockerfile',
    'api/image.py',
    'dcx_instructions.py',
    'image_protocol.py',
    'image_service/__init__.py',
    'image_service/__main__.py',
    'image_service/app.py',
    'image_service/ash_map.py',
    'image_service/ash_reports.py',
    'image_service/assets/preview/DCX_WARDS_NOTICE.txt',
    'image_service/assets/preview/FLOOD_NOTICE.txt',
    'image_service/assets/preview/MUNICIPALITIES_NOTICE.txt',
    'image_service/assets/preview/NWP_NOTICE.txt',
    'image_service/assets/preview/PREFECTURE_BOUNDARIES_NOTICE.txt',
    'image_service/assets/preview/dcx_wards.geojson.zlib',
    'image_service/assets/preview/dcx_wards_manifest.json',
    'image_service/assets/preview/flood.geojson.zlib',
    'image_service/assets/preview/flood_manifest.json',
    'image_service/assets/preview/municipalities.geojson.zlib',
    'image_service/assets/preview/municipalities_manifest.json',
    'image_service/assets/preview/nwp_manifest.json',
    'image_service/assets/preview/nwp_points.json',
    'image_service/assets/preview/prefecture_boundaries.geojson.zlib',
    'image_service/assets/preview/prefecture_boundaries_manifest.json',
    'image_service/assets/v1/FONT_NOTICE.txt',
    'image_service/assets/v1/NATURAL_EARTH.txt',
    'image_service/assets/v1/NotoSansCJKjp-Bold.otf',
    'image_service/assets/v1/NotoSansCJKjp-Regular.otf',
    'image_service/assets/v1/OFL.txt',
    'image_service/assets/v1/land.geojson',
    'image_service/assets/v1/manifest.json',
    'image_service/assets/v2/JMA_NOTICE.txt',
    'image_service/assets/v2/groups.json',
    'image_service/assets/v2/manifest.json',
    'image_service/assets/v2/marine.geojson',
    'image_service/assets/v2/prefectures.geojson',
    'image_service/assets/v2/tsunami.geojson',
    'image_service/assets/v3/LAND_NOTICE.txt',
    'image_service/assets/v3/REGIONAL_NOTICE.txt',
    'image_service/assets/v3/VOLCANO_NOTICE.txt',
    'image_service/assets/v3/eew.geojson.zlib',
    'image_service/assets/v3/eew_groups.json',
    'image_service/assets/v3/land.geojson.zlib',
    'image_service/assets/v3/land_manifest.json',
    'image_service/assets/v3/regional_manifest.json',
    'image_service/assets/v3/volcano.json',
    'image_service/assets/v3/volcano_manifest.json',
    'image_service/assets/v3/weather.geojson.zlib',
    'image_service/assets/v3/weather_groups.json',
    'image_service/eew_map.py',
    'image_service/flood_map.py',
    'image_service/flood_reports.py',
    'image_service/map_base_v3.py',
    'image_service/map_v3.py',
    'image_service/map_v4.py',
    'image_service/marine_regions.py',
    'image_service/nwp_map.py',
    'image_service/nwp_reports.py',
    'image_service/preview_cancel_card.py',
    'image_service/preview_cancel_map.py',
    'image_service/preview_cancel_scope.py',
    'image_service/preview_cancellation.py',
    'image_service/preview_context.py',
    'image_service/preview_dcx_areas.py',
    'image_service/preview_dcx_geometry.py',
    'image_service/preview_dcx_reports.py',
    'image_service/preview_diagnostic_errors.py',
    'image_service/preview_diagnostics.py',
    'image_service/preview_geometry_cache.py',
    'image_service/preview_labels.py',
    'image_service/preview_nankai.py',
    'image_service/preview_place_labels.py',
    'image_service/preview_points.py',
    'image_service/preview_regions.py',
    'image_service/preview_scale.py',
    'image_service/preview_wide_dcx.py',
    'image_service/preview_wide_flood.py',
    'image_service/preview_wide_layout.py',
    'image_service/preview_wide_points.py',
    'image_service/preview_wide_regions.py',
    'image_service/preview_wide_special.py',
    'image_service/preview_wide_tsunami.py',
    'image_service/region_map.py',
    'image_service/regional_reports.py',
    'image_service/render.py',
    'image_service/render_preview.py',
    'image_service/render_v2.py',
    'image_service/render_v3.py',
    'image_service/render_v4.py',
    'image_service/volcano_map.py',
    'image_service/weather_map.py',
    'image_service/weather_map_v4.py',
    'image_service/webp_w1.py',
    'image_versions.py',
    'licenses/pyserial-3.5-LICENSE.txt',
    'nankai_image.py',
    'requirements.txt',
    'vercel.json',
)


def is_legacy(manifest):
    return 'receiver/main.py' in manifest['files']


def profile_path(manifest):
    return 'image_versions.py' if is_legacy(manifest) else PROFILE_PATH


def commands(legacy=False):
    if legacy:
        return {'shirasu': ['python', './main.py'],
                'metrics': ['python', 'metrics.py'],
                'backup': ['python', 'backup_worker.py']}
    return {'shirasu': ['python', '-m', 'shirasu'],
            'metrics': ['python', '-m', 'shirasu.metrics'],
            'backup': ['python', '-m', 'shirasu.backup_worker']}


def healthchecks(legacy=False):
    commands_ = commands(legacy)
    return {'shirasu': ['CMD', *commands_['shirasu'], '--health'],
            'backup': ['CMD', *commands_['backup'], '--health']}
