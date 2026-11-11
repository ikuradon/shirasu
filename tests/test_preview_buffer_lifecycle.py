"""Preview clear releases owned Agg buffers without global GC or pixel changes."""

from contextlib import ExitStack
from dataclasses import dataclass
import gc
import importlib.util
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib import patheffects
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.offsetbox import AnnotationBbox, TextArea
    from matplotlib.text import Text
    from image_service import preview_cancel_map as cancellation
    from image_service import preview_wide_dcx as dcx
    from image_service import preview_wide_layout as wide
    from image_service import render_preview as preview


@dataclass(frozen=True)
class CandidatePlan:
    geometries: tuple = (True,)
    areas: tuple = (True,)
    reasons: tuple = ()
    scope_kind: str = 'geometry'


class SyntheticReport:
    pass


@unittest.skipUnless(HAS_RENDERER, 'Install the isolated image renderer dependencies')
class PreviewBufferLifecycleTests(unittest.TestCase):
    def setUp(self):
        # A collection must not hide an owned reference or make this test pass.
        enabled = gc.isenabled()
        gc.disable()
        self.addCleanup(gc.enable if enabled else gc.disable)
        self.enterContext(patch.object(gc, 'collect', side_effect=AssertionError('No global GC')))

    def figure(self, *, ordinary=False):
        if ordinary:
            fig = Figure(figsize=(3, 2), dpi=wide.DPI, facecolor=wide.SEA)
            FigureCanvasAgg(fig)
        else:
            fig = wide.figure()
            fig.set_size_inches(3, 2)
        self.addCleanup(fig.clear)
        return fig

    def scene(self, fig):
        ax = fig.add_axes((.12, .15, .8, .7))
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.plot((.1, .9), (.2, .8), color='#ee8833')
        label = ax.text(.15, .6, 'area', color='#5577aa')
        label.set_path_effects([patheffects.withStroke(linewidth=1, foreground='white')])
        ax.add_artist(AnnotationBbox(TextArea('nested label'), (.7, .35), frameon=True))
        fig.text(.1, .9, 'preview')
        return ax

    def png(self, fig):
        stream = io.BytesIO()
        fig.savefig(stream, format='png', metadata={'Software': 'buffer-lifecycle-test'})
        return stream.getvalue()

    def assert_released(self, fig, reference):
        self.assertIsNone(reference())
        self.assertNotIn('renderer', vars(fig.canvas))
        self.assertIsNone(fig.canvas._lastKey)
        self.assertFalse(gc.isenabled())

    def test_clear_releases_agg_immediately_and_leaves_ordinary_figures_unchanged(self):
        owned, ordinary = self.figure(), self.figure(ordinary=True)
        for fig in (owned, ordinary):
            self.scene(fig)
            fig.canvas.draw()
        released = weakref.ref(owned.canvas.get_renderer())
        retained = weakref.ref(ordinary.canvas.get_renderer())
        labels = owned.findobj(Text)
        owned.clear()
        ordinary.clear()
        self.assert_released(owned, released)
        self.assertTrue(all(label._renderer is None for label in labels))
        self.assertIsNotNone(retained(), 'The ordinary Matplotlib lifecycle must not be patched globally')

    def test_extent_only_renderer_and_repeated_clear_are_safe_before_any_draw(self):
        fig = self.figure()
        fig.clear()
        label = fig.text(.2, .5, 'measure before drawing')
        label.get_window_extent()
        reference = weakref.ref(fig.canvas.get_renderer())
        self.assertIs(label._renderer, reference())
        fig.clear(keep_observers=True)
        self.assert_released(fig, reference)
        self.assertIsNone(label._renderer)
        fig.clear()
        self.assertNotIn('renderer', vars(fig.canvas))

    def test_clear_and_redraw_preserve_png_and_annotation_rendering(self):
        fig, ordinary = self.figure(), self.figure(ordinary=True)
        self.scene(ordinary)
        expected = self.png(ordinary)
        canvas = fig.canvas
        for _ in range(3):
            self.scene(fig)
            self.assertEqual(self.png(fig), expected)
            reference = weakref.ref(fig.canvas.get_renderer())
            fig.clear()
            self.assert_released(fig, reference)
            self.assertIs(fig.canvas, canvas)

    def test_saved_figure_capture_can_still_measure_and_redraw_before_explicit_clear(self):
        fig = self.figure()
        original_clear, original_save = fig.clear, fig.savefig
        saved = False

        def keep_after_save(*args, **kwargs):
            if not saved:
                original_clear(*args, **kwargs)

        def save_and_keep(*args, **kwargs):
            nonlocal saved
            result = original_save(*args, **kwargs)
            saved = True
            return result

        self.scene(fig)
        with patch.object(fig, 'clear', keep_after_save), patch.object(fig, 'savefig', save_and_keep):
            expected = self.png(fig)
            reference = weakref.ref(fig.canvas.get_renderer())
            fig.clear()
            self.assertIsNotNone(reference())
            for label in fig.findobj(Text):
                label.get_window_extent(fig.canvas.get_renderer())
            self.assertEqual(self.png(fig), expected)
        fig.clear()
        self.assert_released(fig, reference)

    def renderer_context(self, stack, drawer, *, nankai=False):
        figures, references = [], []
        make_figure, finish = wide.figure, preview._finish

        def create():
            fig = make_figure()
            fig.set_size_inches(3, 2)
            figures.append(fig)
            self.addCleanup(fig.clear)
            return fig

        def saved(fig, category, decision):
            result = finish(fig, category, decision)
            references.append(weakref.ref(fig.canvas.get_renderer()))
            return result

        stack.enter_context(patch.object(wide, 'figure', create))
        stack.enter_context(patch.object(preview, 'verify_assets'))
        stack.enter_context(patch.object(preview, 'is_nankai_entry', return_value=nankai))
        stack.enter_context(patch.object(preview.base, '_decode', return_value=(SyntheticReport(),)))
        stack.enter_context(patch.object(preview.base, '_metadata', return_value=('', '', '', False)))
        reset = stack.enter_context(patch.object(preview.base, '_reset_assembly'))
        stack.enter_context(patch.object(preview, 'DRAWERS', {SyntheticReport: drawer}))
        stack.enter_context(patch.object(wide, 'context_names'))
        stack.enter_context(patch.object(wide, 'validate', side_effect=lambda fig: fig.canvas.draw()))
        stack.enter_context(patch.object(wide, 'summary', side_effect=lambda fig, reports: self.scene(fig)))
        stack.enter_context(patch.object(preview, '_finish', saved))
        return figures, references, reset

    def test_normal_render_and_valueerror_fallback_release_every_renderer(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), ExitStack() as stack:
                failed = []

                def draw(fig, reports):
                    self.scene(fig)
                    fig.canvas.draw()
                    if fallback:
                        # The exception traceback temporarily owns this local.
                        renderer = fig.canvas.get_renderer()
                        failed.append(weakref.ref(renderer))
                        raise ValueError('synthetic layout failure')

                figures, references, reset = self.renderer_context(stack, draw)
                image = preview.render((('synthetic', 0),))
                self.assertEqual(image.decision.mode, 'summary' if fallback else 'map')
                self.assertTrue(image.png.startswith(b'\x89PNG'))
                self.assertEqual(reset.call_count, 2)
                self.assertTrue(all(reference() is None for reference in (*failed, *references)))
                self.assertNotIn('renderer', vars(figures[0].canvas))

    def test_fatal_draw_and_png_failures_still_release_owned_buffers(self):
        for phase in ('draw', 'save'):
            with self.subTest(phase=phase), ExitStack() as stack:
                references = []

                def fail(fig, *args):
                    fig.canvas.draw()
                    renderer = fig.canvas.get_renderer()
                    references.append(weakref.ref(renderer))
                    raise RuntimeError('synthetic fatal failure')

                def draw(fig, reports):
                    self.scene(fig)
                    if phase == 'draw':
                        fail(fig)

                figures, _, reset = self.renderer_context(stack, draw)
                if phase == 'save':
                    stack.enter_context(patch.object(preview, '_finish', fail))
                with self.assertRaisesRegex(RuntimeError, 'synthetic fatal failure'):
                    preview.render((('synthetic', 0),))
                self.assertTrue(all(reference() is None for reference in references))
                self.assertNotIn('renderer', vars(figures[0].canvas))
                self.assertEqual(reset.call_count, 2)

    def test_nankai_header_path_uses_the_same_buffer_cleanup(self):
        with ExitStack() as stack:
            figures, references, reset = self.renderer_context(stack, None, nankai=True)
            stack.enter_context(patch.object(preview, 'nankai_header', return_value=SimpleNamespace(information=0)))
            stack.enter_context(patch.object(preview.nankai, 'draw_card', side_effect=lambda fig, header: self.scene(fig)))
            stack.enter_context(patch.object(preview.nankai, 'validate', side_effect=lambda fig: fig.canvas.draw()))
            image = preview.render((('synthetic nankai', 0),))
        self.assertEqual(image.category, 'NankaiTroughEarthquake')
        self.assertEqual(image.decision.mode, 'topic')
        self.assert_released(figures[0], references[0])
        reset.assert_not_called()

    def test_cancellation_reference_figures_release_on_success_and_failure(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid), ExitStack() as stack:
                figures, references, _ = self.renderer_context(stack, None)

                def draw(fig, reports):
                    ax = self.scene(fig)
                    fig.canvas.draw()
                    references.append(weakref.ref(fig.canvas.get_renderer()))
                    if invalid:
                        raise ValueError('invalid original map')
                    ax._wide_map_view = SimpleNamespace(ax=ax, locator=None,
                        visible_limits=(*ax.get_xlim(), *ax.get_ylim()), places=())

                if invalid:
                    with self.assertRaisesRegex(ValueError, 'invalid original map'):
                        cancellation.capture_frame(draw, ('original',))
                else:
                    frame = cancellation.capture_frame(draw, ('original',))
                    self.assertEqual(frame.limits, (0., 1., 0., 1.))
                self.assert_released(figures[0], references[0])

    def test_dcx_candidate_failure_releases_its_renderer_before_next_candidate(self):
        fig, references = self.figure(), []

        def fail(fig, report, plan):
            self.scene(fig)
            fig.canvas.draw()
            renderer = fig.canvas.get_renderer()
            references.append(weakref.ref(renderer))
            raise ValueError('synthetic projection failure')

        def succeed(fig, report, plan):
            self.assertIsNone(references[0]())
            self.scene(fig)

        with patch.object(dcx, 'dcx_plan', return_value=CandidatePlan()), \
                patch.object(dcx, '_geometry', side_effect=fail), \
                patch.object(dcx, '_area', side_effect=succeed), \
                patch.object(wide, 'validate', side_effect=lambda fig: fig.canvas.draw()):
            plan = dcx.draw_dcx(fig, (SyntheticReport(),))
        self.assertEqual(plan.scope_kind, 'area')
        reference = weakref.ref(fig.canvas.get_renderer())
        fig.clear()
        self.assert_released(fig, reference)


if __name__ == '__main__':
    unittest.main()
