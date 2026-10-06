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

from main import PulsePlayer, Visualizer, resolve_audio_output  # noqa: E402


class LibmpvTest(unittest.TestCase):
    def test_libmpv_is_discoverable(self):
        # main._prepare_libmpv() has to make this work, otherwise `import mpv`
        # raises OSError before the app even starts
        self.assertIsNotNone(ctypes.util.find_library("mpv"))


class AudioOutputTest(unittest.TestCase):
    def test_explicit_override_wins(self):
        with mock.patch.dict(os.environ, {"PULSE_AO": "null"}):
            self.assertEqual(resolve_audio_output(), "null")

    def test_falls_back_to_something_playable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotEqual(resolve_audio_output(), "pulse")


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
