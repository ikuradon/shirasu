"""Unpublished Nankai headline cards, with no map or assembled bulletin text."""

from datetime import datetime

from nankai_image import NankaiHeader
from image_service import preview_wide_layout as wide
from image_service import render as base


STATUS_COLORS = {
    '調査中': '#c3e6f2',
    '巨大地震警戒': '#ff7777',
    '巨大地震注意': '#f3b747',
    '調査終了': '#a6adb5',
    'その他の情報': wide.WHITE,
    '取消': '#d0d5db',
}
CREDIT = '情報：気象庁 / 受信：みちびき'


def draw_card(fig, header):
    """Use strictly validated header fields; never read pages or bulletin text."""
    if (not isinstance(header, NankaiHeader)
            or not isinstance(header.issued_at, datetime) or header.issued_at.utcoffset() is None
            or header.classification not in (1, 2, 3, 7) or header.information not in (0, 1, 2)
            or header.serial not in (1, 2, 3, 4, 5, 6, 15)):
        raise ValueError('Invalid Nankai headline metadata')
    if fig.axes:
        raise ValueError('Nankai headline cards must not retain a map')

    wide.panel(fig)
    if header.classification == 7:
        badge = wide._rect(fig, .04, .875, .30, .09, wide.ALERT)
        badge.set_gid('nankai-training-badge')
        wide.text(fig, .055, .92, '【訓練・試験】', 36, wide.SEA, bold=True,
                  va='center', gid='nankai-training')

    wide.text(fig, .045, .775, header.title, 36, bold=True,
              va='center', gid='nankai-title')
    keyword = header.keyword
    for size in (104, 100, 96):
        if wide._width(fig, keyword, size, True) <= wide.WIDTH * .67:
            break
    else:
        raise ValueError('Nankai headline does not fit')
    wide.text(fig, .045, .51, keyword, size, STATUS_COLORS[keyword], bold=True,
              va='center', gid='nankai-topic')
    wide.text(fig, .045, .13, '詳細は投稿本文を参照', 21, wide.MUTED,
              va='center', gid='nankai-footer')
    wide.text(fig, .022, .022, CREDIT, 10, wide.MUTED,
              va='center', gid='nankai-credit')

    at = header.issued_at.astimezone(base.JST)
    wide.text(fig, wide.PANEL_LEFT, .875, '発表日時', 19, wide.MUTED,
              va='center', gid='nankai-sidebar-time-label')
    wide.text(fig, wide.PANEL_LEFT, .805, at.strftime('%Y/%m/%d'), 26, bold=True,
              va='center', gid='nankai-sidebar-date')
    wide.text(fig, wide.PANEL_LEFT, .735, at.strftime('%H:%M'), 36, bold=True,
              va='center', gid='nankai-sidebar-time')
    wide.text(fig, wide.PANEL_RIGHT, .735, 'JST', 18, wide.MUTED,
              ha='right', va='center', gid='nankai-sidebar-zone')
    wide.text(fig, wide.PANEL_LEFT, .56, '情報区分', 19, wide.MUTED,
              va='center', gid='nankai-sidebar-type-label')
    wide.text(fig, wide.PANEL_LEFT, .49, {0: '発表', 1: '訂正', 2: '取消'}[header.information],
              32, bold=True, va='center', gid='nankai-sidebar-type')


def validate(fig):
    """Check the complete card, including the large headline and training badge."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    labels = list(fig.texts)
    for index, label in enumerate(labels):
        box = label.get_window_extent(renderer)
        if box.x0 < 0 or box.y0 < 0 or box.x1 > wide.WIDTH or box.y1 > wide.HEIGHT:
            raise ValueError('Nankai headline text leaves the image')
        if (label.get_gid() or '').startswith('nankai-'):
            if 'sidebar' in label.get_gid():
                if box.x0 < wide.PANEL_X * wide.WIDTH:
                    raise ValueError('Nankai sidebar text leaves the panel')
            elif box.x1 >= wide.PANEL_X * wide.WIDTH:
                raise ValueError('Nankai headline text enters the panel')
        if any(box.overlaps(other.get_window_extent(renderer)) for other in labels[index + 1:]):
            raise ValueError('Nankai headline text overlaps')
