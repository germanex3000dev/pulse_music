"""Headless tests for PULSE.

Started out of Nix pain: on NixOS ``libmpv`` is not in the loader cache, so
python-mpv cannot load it and PULSE dies at import time. These tests run in
``nix flake check`` to make sure that cannot come back unnoticed, and to keep
the layout regressions from returning.

They need no terminal, no music and no CAVA: the spectrum is injected by hand
and CAVA is never spawned.
"""

import asyncio
import ctypes.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from textual.containers import Vertical  # noqa: E402
from textual.widgets import Input, Tree  # noqa: E402

import main  # noqa: E402
from main import PulsePlayer, Visualizer, resolve_audio_output  # noqa: E402

# big enough that the 68% / 100% split is unambiguous
SCREEN = (120, 30)
TABS = 6


class PulseTestCase(unittest.TestCase):
    """Shared helpers for the async Textual tests."""

    def run_app(self, body, size=SCREEN, music=None):
        """Run `body(app, pilot)` against a headless PULSE, then return."""

        async def scenario():
            app = PulsePlayer(music or [])
            async with app.run_test(size=size) as pilot:
                await body(app, pilot)

        asyncio.run(scenario())

    async def settle(self, pilot, widget, tries=80):
        """Pause until the widget stops being resized by pending layout."""
        last = None
        for _ in range(tries):
            await pilot.pause()
            if widget.size == last:
                return last
            last = widget.size
        return last

    def freeze_spectrum(self, app, ramp=False):
        """Pin the visualizer to a known spectrum and stop CAVA entirely."""
        viz = app.query_one(Visualizer)
        viz._tick = lambda: None
        if ramp:
            values = [((i % 12) + 1) / 13 for i in range(main.CAVA_BARS)]
        else:
            values = [0.9] * main.CAVA_BARS
        viz._smooth = list(values)
        viz._latest = list(values)
        viz._cava_missing = False
        return viz


class LibmpvTest(PulseTestCase):
    def test_libmpv_is_discoverable(self):
        # main._prepare_libmpv() has to make this work, otherwise `import mpv`
        # raises OSError before the app even starts
        self.assertIsNotNone(ctypes.util.find_library("mpv"))

    def test_libmpv_is_actually_usable(self):
        # finding the name is not enough: a libmpv with unmet dependencies
        # fails at CDLL time instead
        found = ctypes.util.find_library("mpv")
        self.assertIsNotNone(ctypes.CDLL(found))

    def test_explicit_override_is_first_candidate(self):
        override = "/custom/libmpv.so.2"
        with mock.patch.dict(os.environ, {"PULSE_LIBMPV": override}):
            self.assertEqual(str(main._libmpv_candidates()[0]), override)


class AudioOutputTest(PulseTestCase):
    def test_explicit_override_wins(self):
        with mock.patch.dict(os.environ, {"PULSE_AO": "null"}):
            self.assertEqual(resolve_audio_output(), "null")

    def test_falls_back_to_something_playable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotEqual(resolve_audio_output(), "pulse")


class LibraryToggleLayoutTest(PulseTestCase):
    """TAB must resize now-playing within the screen, never past its edge.

    Regression test: the width class was applied on the inverted condition, so
    hiding the library left now-pane at 68% (dead gap on the right) and showing
    it again jumped to 100% starting at x=38 - 158 columns on a 120-column
    terminal. #main-row clips overflow, so the right end of the spectrum
    vanished instead of being reported as an error.
    """

    def test_no_overflow_and_idempotent(self):
        async def body(app, pilot):
            now = app.query_one("#now-pane", Vertical)
            viz = app.query_one(Visualizer)
            width = SCREEN[0]
            seen = []

            for step in range(TABS):
                if step:
                    await pilot.press("s")
                await self.settle(pilot, viz)

                region = now.region
                self.assertLessEqual(
                    region.x + region.width,
                    width,
                    f"step {step}: now-pane overflows the screen: {region}",
                )
                seen.append((region.x, region.width, tuple(viz.size)))

            # even steps have the library visible and must look identical
            even = [s for i, s in enumerate(seen) if i % 2 == 0]
            for entry in even[1:]:
                self.assertEqual(entry, even[0], f"toggle is not idempotent: {entry}")

            # odd steps have it hidden, so now-pane owns the whole row
            hidden = [s for i, s in enumerate(seen) if i % 2 == 1]
            self.assertEqual(hidden[0][0], 0, "hidden library should start at x=0")
            self.assertEqual(
                hidden[0][1], width, "hidden library should use full width"
            )

        self.run_app(body)


class VisualizerFitTest(PulseTestCase):
    """Bars must stay inside the visualizer box at every width."""

    def test_bars_never_escape_the_region(self):
        async def body(app, pilot):
            viz = self.freeze_spectrum(app)

            async def bar_extent():
                lo = hi = None
                for strip in app.screen._compositor.render_strips():
                    text = "".join(seg.text for seg in strip)
                    cols = [i for i, c in enumerate(text) if c in main.BAR_GLYPHS]
                    if cols:
                        lo = cols[0] if lo is None else min(lo, cols[0])
                        hi = cols[-1] if hi is None else max(hi, cols[-1])
                return lo, hi

            for step in range(TABS):
                if step:
                    await pilot.press("s")
                await self.settle(pilot, viz)

                region = viz.region
                lo, hi = await bar_extent()
                self.assertIsNotNone(lo, f"step {step}: no bars drawn at all")
                self.assertGreaterEqual(lo, region.x, f"step {step}: bars left the box")
                self.assertLessEqual(
                    hi, region.x + region.width, f"step {step}: bars right of the box"
                )

        self.run_app(body)

    def test_search_filters_tracks_and_plays_the_first_hit(self):
        tracks = ["/m/Air.mp3", "/m/Blue.mp3", "/m/Red.mp3"]

        async def body(app, pilot):
            tree = app.query_one(Tree[str])

            def visible():
                tracks_node = next(
                    n for n in tree.root.children if str(n.label) == "Tracks"
                )
                return [str(c.label) for c in tracks_node.children]

            self.assertEqual(visible(), ["Air.mp3", "Blue.mp3", "Red.mp3"])

            await pilot.press("/")
            for key in "re":
                await pilot.press(key)
            self.assertEqual(visible(), ["Red.mp3"])

            # Esc closes the field and the filter it applied goes with it
            await pilot.press("escape")
            self.assertIsNone(app.focused)
            self.assertTrue(app.query_one("#lib-search", Input).has_class("-hidden"))
            self.assertEqual(visible(), ["Air.mp3", "Blue.mp3", "Red.mp3"])

            # hidden field must not steal keys from the rest of the app
            await pilot.press("s")
            await pilot.pause()
            self.assertTrue(
                app.query_one("#library-pane", Vertical).has_class("-hidden"),
                "a hidden search field swallowed the S keybind",
            )
            await pilot.press("s")
            await pilot.pause()

            await pilot.press("/")
            for key in "blue":
                await pilot.press(key)
            await pilot.press("enter")
            self.assertEqual(app.current_path, "/m/Blue.mp3")
            self.assertTrue(app.query_one("#lib-search", Input).has_class("-hidden"))
            self.assertEqual(visible(), ["Air.mp3", "Blue.mp3", "Red.mp3"])

        self.run_app(body, music=tracks)

    def test_spectrum_survives_hiding_the_visualizer(self):
        # V collapses the box to zero; S afterwards must not resurrect a
        # stale width
        async def body(app, pilot):
            viz = self.freeze_spectrum(app)
            await pilot.press("v")
            await pilot.press("s")
            await pilot.press("s")
            await self.settle(pilot, viz)
            self.assertEqual(viz.size.width, 0)

            await pilot.press("v")
            await self.settle(pilot, viz)
            self.assertGreater(viz.size.width, 0, "viz did not come back")

        self.run_app(body)


class PulseAppTest(PulseTestCase):
    def test_mounts_and_survives_keybindings(self):
        async def body(app, pilot):
            await pilot.pause()
            self.assertIsNotNone(app.player, "mpv was never created")
            self.assertIsNotNone(app.query_one(Visualizer))

            for key in ("space", "tab", "v", "r", "m", "n", "p", "up", "down"):
                await pilot.press(key)
            await pilot.pause()

            await pilot.press("q")
            self.assertFalse(app.is_running)

        self.run_app(body)


if __name__ == "__main__":
    unittest.main()
