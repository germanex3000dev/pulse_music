"""Headless smoke test for PULSE.

Started out of Nix pain: on NixOS ``libmpv`` is not in the loader cache, so
python-mpv cannot load it and PULSE dies at import time. These tests run in
``nix flake check`` to make sure that cannot come back unnoticed.
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

import main  # noqa: E402
from main import PulsePlayer, Visualizer, resolve_audio_output  # noqa: E402


class LibmpvTest(unittest.TestCase):
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
        with mock.patch.dict(os.environ, {"PULSE_LIBMPV": "/custom/libmpv.so.2"}):
            self.assertEqual(str(main._libmpv_candidates()[0]), "/custom/libmpv.so.2")


class AudioOutputTest(unittest.TestCase):
    def test_explicit_override_wins(self):
        with mock.patch.dict(os.environ, {"PULSE_AO": "null"}):
            self.assertEqual(resolve_audio_output(), "null")

    def test_falls_back_to_something_playable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotEqual(resolve_audio_output(), "pulse")


class LibraryToggleLayoutTest(unittest.TestCase):
    """TAB must resize now-playing within the screen, never past its edge.

    Regression test: the width class was applied on the inverted condition, so
    hiding the library left now-pane at 68% (dead gap) and showing it again
    jumped to 100% starting at x=38 - 158 columns on a 120-column terminal.
    #main-row clips overflow, so the right end of the spectrum vanished.
    """

    async def scenario(self):
        app = PulsePlayer([])
        async with app.run_test(size=(120, 30)) as pilot:
            now = app.query_one("#now-pane", Vertical)
            viz = app.query_one(Visualizer)

            async def settle():
                last = None
                for _ in range(80):
                    await pilot.pause()
                    if viz.size == last:
                        return last
                    last = viz.size
                return last

            widths = []
            for step in range(6):
                if step:
                    await pilot.press("tab")
                await settle()
                region = now.region
                self.assertLessEqual(
                    region.x + region.width,
                    120,
                    f"step {step}: now-pane overflows the screen: {region}",
                )
                widths.append((region.x, region.width, tuple(viz.size)))

            # every even step has the library visible and must look identical
            even = [w for i, w in enumerate(widths) if i % 2 == 0]
            for entry in even[1:]:
                self.assertEqual(entry, even[0], f"toggle is not idempotent: {entry}")

            # and the hidden state must actually use the full width
            odd = [w for i, w in enumerate(widths) if i % 2 == 1]
            self.assertEqual(odd[0][0], 0, "hidden library should start at x=0")
            self.assertEqual(odd[0][1], 120, "hidden library should use full width")

    def test_no_overflow_and_idempotent(self):
        asyncio.run(self.scenario())


class PulseAppTest(unittest.TestCase):
    def test_mounts_and_survives_keybindings(self):
        async def scenario():
            app = PulsePlayer([])
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                self.assertIsNotNone(app.player, "mpv was never created")
                self.assertIsNotNone(app.query_one(Visualizer))

                for key in ("space", "tab", "v", "r", "m", "n", "p", "up", "down"):
                    await pilot.press(key)
                await pilot.pause()

                await pilot.press("q")
            self.assertFalse(app.is_running)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
