"""Image generations included in this source distribution.

The regular checkout only exposes published renderers. The release-candidate
builder writes a separate, explicit profile into its isolated artifacts; no
environment variable or request can register an unpublished renderer here.
"""

RENDERERS = {'v1': 'image_service.render', 'v2': 'image_service.render_v2',
             'v3': 'image_service.render_v3', 'v4': 'image_service.render_v4'}
SUPPORTED_VERSIONS = tuple(RENDERERS)
NANKAI_TOPIC_VERSIONS = ()
# Only isolated release profiles can opt a renderer into the frozen w1 codec.
WEBP_VERSIONS = ()
CANDIDATE_VERSION = None
