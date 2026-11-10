"""Measured labels with per-row badges for unpublished forecast maps."""

from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, PaddedBox, TextArea, VPacker

from image_service import region_map as layout
from image_service import render as base


def draw_labels(fig, ax, anchors, notices, *, prefix, obstacles=()):
    if not 1 <= len(notices) <= 16:
        raise ValueError('Too many forecast labels for a readable map')
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    points = [fig.transFigure.inverted().transform(ax.transData.transform(point)) for point in anchors]
    outlines = [(artist.get_transform().transform_path(artist.get_path()), artist.get_linewidth())
                for artist in (*ax.patches, *ax.lines)
                if (artist.get_gid() or '').startswith(('region-area-', 'region-coast-', 'flood-casing-'))]
    boundaries = layout._boundary_mask(outlines) if outlines else None
    for scale in (1, .85, .7):
        boxes, sizes = [], []
        for notice in notices:
            children = [TextArea(text, textprops={'fontproperties': base._font(21 * scale, True),
                        'color': notice.color, 'parse_math': False})
                        for text in base._wrap(notice.title, 36)]
            for index, (line, style) in enumerate(zip(notice.lines, notice.styles, strict=True)):
                text = TextArea('\n'.join(base._wrap(line, 44)), textprops={
                    'fontproperties': base._font(16 * scale, True), 'color': style.foreground,
                    'parse_math': False, 'gid': f'{prefix}-{notice.code}-{index}'})
                children.append(PaddedBox(text, pad=4, draw_frame=True, patch_attrs={
                    'facecolor': style.background, 'edgecolor': style.edge, 'linewidth': 1.1,
                    'boxstyle': 'square,pad=0'}))
            box = VPacker(children=children, align='left', pad=0, sep=6)
            box.set_figure(fig)
            extent = box.get_window_extent(renderer)
            boxes.append(box)
            sizes.append(((extent.width + 24) / base.WIDTH, (extent.height + 24) / base.HEIGHT))
        try:
            rectangles = layout._place_labels(points, sizes, boundaries=boundaries, obstacles=obstacles)
        except ValueError:
            continue
        for point, projected, box, rectangle in zip(anchors, points, boxes, rectangles, strict=True):
            centre = ((rectangle[0] + rectangle[2]) / 2, (rectangle[1] + rectangle[3]) / 2)
            start, end = base._leader_segment(projected, rectangle)
            ax.add_line(Line2D(*zip(start, end), transform=fig.transFigure, color='#899cac',
                              linewidth=1, zorder=7, gid='label-leader'))
            ax.add_artist(AnnotationBbox(box, point, xycoords='data', xybox=centre,
                          boxcoords=fig.transFigure, frameon=False, box_alignment=(.5, .5), zorder=9))
        return rectangles
    raise ValueError('Forecast labels do not fit')
