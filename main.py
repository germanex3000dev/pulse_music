import argparse
import ctypes
import ctypes.util
import glob
import importlib
import locale
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

locale.setlocale(locale.LC_NUMERIC, "C")


# ---------------------------------------------------------------------------
# platform bootstrap
#
# Nix keeps every library in its own store path and ships an almost empty
# loader cache, so a few things that work out of the box on a normal distro
# have to be wired up by hand. Everything here is best effort: on a regular
# system these functions find what they need immediately and do nothing.
# ---------------------------------------------------------------------------

#: mpv audio outputs PULSE understands, most preferred first.
AUDIO_OUTPUT_PREFERENCE = ("pipewire", "pulse", "alsa")


def _ensure_runtime_dir() -> None:
    """Point audio clients at the session socket when the environment forgot to.

    PipeWire and PulseAudio both look for their socket below
    ``$XDG_RUNTIME_DIR``. Launching PULSE from a service manager, a bare
    ``nix run`` or a container often leaves that variable unset.
    """
    if os.environ.get("XDG_RUNTIME_DIR"):
        return
    runtime_dir = f"/run/user/{os.getuid()}"
    if os.path.isdir(runtime_dir):
        os.environ["XDG_RUNTIME_DIR"] = runtime_dir


def _libmpv_candidates() -> list[Path]:
    """List every place libmpv may hide when the loader cannot find it.

    Ordered best first: an explicit override, then the usual library
    directories, then the Nix store. In the store we prefer the libmpv that
    belongs to the mpv binary on PATH, since that is the one PULSE will end
    up talking to.
    """
    candidates: list[Path] = []

    # explicit escape hatch: PULSE_LIBMPV=/path/to/libmpv.so.2
    override = os.environ.get("PULSE_LIBMPV")
    if override:
        candidates.append(Path(override).expanduser())

    for directory in (
        "/usr/lib",
        "/usr/lib64",
        "/usr/local/lib",
        "/lib",
        "/lib64",
        # NixOS system profile, i.e. whatever environment.systemPackages links
        "/run/current-system/sw/lib",
    ):
        candidates.extend(
            Path(directory) / name for name in ("libmpv.so.2", "libmpv.so")
        )

    # Nix store: the mpv binary on PATH decides first, so PULSE never mixes a
    # libmpv from one mpv build with the `mpv --ao=help` answer from another
    mpv_binary = shutil.which("mpv")
    if mpv_binary:
        lib_dir = Path(mpv_binary).resolve().parent.parent / "lib"
        candidates.extend(lib_dir / name for name in ("libmpv.so.2", "libmpv.so"))

    # then any mpv derivation, then anything else shipping libmpv
    candidates.extend(
        Path(p) for p in sorted(glob.glob("/nix/store/*mpv*/lib/libmpv.so.2"))
    )
    candidates.extend(
        Path(p) for p in sorted(glob.glob("/nix/store/*/lib/libmpv.so.2"))
    )
    return candidates


def _prepare_libmpv() -> None:
    """Make ``import mpv`` work where the loader cache knows nothing about libmpv.

    python-mpv loads libmpv through ``ctypes.util.find_library``, which only
    consults ldconfig and ``ld``. On NixOS both come up empty even with mpv
    installed, so we locate the library ourselves, load it once to fail early
    if it is unusable, and hand python-mpv the absolute path.
    """
    _ensure_runtime_dir()

    if ctypes.util.find_library("mpv"):
        return

    for candidate in _libmpv_candidates():
        if not candidate.is_file():
            continue
        try:
            ctypes.CDLL(str(candidate))
        except OSError:
            continue
        found = str(candidate)
        lookup = ctypes.util.find_library

        def find_library(name, *args, _found=found, _lookup=lookup, **kwargs):
            return _found if name == "mpv" else _lookup(name, *args, **kwargs)

        ctypes.util.find_library = find_library
        return


def load_mpv_module():
    """Load python-mpv only when the MPV backend is selected."""
    _prepare_libmpv()
    try:
        return importlib.import_module("mpv")
    except OSError as exc:  # no libmpv anywhere we know about
        raise RuntimeError(
            f"{exc}\n"
            "hint: install mpv (on NixOS add `mpv` to environment.systemPackages)\n"
            "      or set PULSE_LIBMPV=/path/to/libmpv.so.2"
        ) from exc

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Header, ProgressBar, Static, Tree

RED_THEME = Theme(
    name="pulse-red",
    primary="#ff3333",
    secondary="#b31212",
    accent="#ff5555",
    warning="#ff5555",
    error="#ff2222",
    success="#ff5555",
    foreground="#ffcfcf",
    background="#000000",
    surface="#0a0a0a",
    panel="#0d0d0d",
    dark=True,
)

AUDIO_EXTS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac",
    ".wav", ".wma", ".aiff", ".aif", ".ape", ".mp4", ".mka",
}

#: PULSE_MUSIC_DIR lets declarative setups (a NixOS module, for instance)
#: pick the library without editing code or passing an argument.
DEFAULT_DIR = Path(os.environ.get("PULSE_MUSIC_DIR") or Path.home() / "Music")

# cava raw output settings (must match the generated cava config)
CAVA_BARS = 64

# Vertical block characters, index = 0..8 (index 0 is blank)
BAR_CHARS = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

#: the visible blocks only, for callers that want to detect drawn bars
BAR_GLYPHS = frozenset(BAR_CHARS[1:])

# Red gradient used for the spectrum: index 0 (bottom, dim) -> N (top, bright)
GRADIENT = [
    "#3a0a0a",
    "#4a0d0d",
    "#5c1010",
    "#701515",
    "#8a1818",
    "#a02020",
    "#c22828",
    "#e03030",
    "#ff3333",
    "#ff5a5a",
    "#ff8080",
    "#ffa4a4",
]


def selected_backend() -> str:
    """Return the configured playback backend, defaulting to MPV."""
    backend = os.environ.get("PULSE_BACKEND", "mpv").strip().lower()
    if backend not in {"mpv", "mpd"}:
        raise ValueError(
            f"unknown backend {backend!r}; choose 'mpv' or 'mpd' with PULSE_BACKEND"
        )
    return backend


def connect_mpd():
    """Connect to MPD using the standard PULSE_MPD_* environment settings."""
    try:
        import mpd
    except ImportError as exc:
        raise RuntimeError(
            "MPD mode requires the python-mpd2 package (called mpd in Python)"
        ) from exc

    host = os.environ.get("PULSE_MPD_HOST", "127.0.0.1")
    port = int(os.environ.get("PULSE_MPD_PORT", "6600"))
    timeout = float(os.environ.get("PULSE_MPD_TIMEOUT", "3"))
    client = mpd.MPDClient()
    client.timeout = timeout
    client.idletimeout = None
    try:
        client.connect(host, port)
        password = os.environ.get("PULSE_MPD_PASSWORD")
        if password:
            client.password(password)
        return client
    except Exception as exc:
        try:
            client.disconnect()
        except Exception:
            pass
        raise RuntimeError(
            f"could not connect to MPD at {host}:{port}: {exc}"
        ) from exc


def mpd_music_files(client) -> list[str]:
    """Return supported, non-hidden file paths from MPD's music database."""
    files = []
    for item in client.listall():
        path = item.get("file")
        if not path or Path(path).suffix.lower() not in AUDIO_EXTS:
            continue
        if any(part.startswith(".") for part in Path(path).parts):
            continue
        files.append(path)
    return sorted(set(files))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pulse",
        description=(
            "A red terminal-styled TUI music player with selectable MPV and MPD backends."
        ),
    )
    parser.add_argument(
        "dir",
        nargs="?",
        default=str(DEFAULT_DIR),
        help=(
            "Directory to scan for audio files "
            f"(default: {DEFAULT_DIR}, scanned recursively)"
        ),
    )
    return parser.parse_args()


def resolve_audio_output() -> str:
    """Pick an mpv audio output this system actually provides.

    Hardcoding ``ao=pulse`` breaks on PipeWire-only setups, and mpv is not
    happy without a working output. Ask mpv what it can do, prefer PipeWire
    (it is the native path on NixOS and keeps CAVA's monitor capture in
    sync), and fall back to letting mpv decide.
    """
    configured = os.environ.get("PULSE_AO")
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["mpv", "--ao=help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "auto"
    if result.returncode != 0:
        return "auto"
    available = {
        words[0]
        for words in (line.split() for line in result.stdout.splitlines())
        if words
    }
    for name in AUDIO_OUTPUT_PREFERENCE:
        if name in available:
            return name
    return "auto"


def scan_music(root: Path) -> list[str]:
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in root.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in AUDIO_EXTS
            and not path.name.startswith(".")
        ):
            files.append(path)
    return [str(p) for p in files]


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_volume(volume: float | None) -> str:
    return f"{int(volume)}%" if volume is not None else "--%"


class Visualizer(Static):
    """An audio-reactive spectrum visualizer that reads raw bar data from cava."""

    DEFAULT_CSS = "Visualizer { height: 1fr; }"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._nbars = CAVA_BARS
        self._latest: list[float] = [0.0] * self._nbars
        self._smooth: list[float] = [0.0] * self._nbars
        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._cava_config: Path | None = None
        self._timer: Timer | None = None
        self._cava_missing = False
        self._warned_about_cava = False

    def on_mount(self) -> None:
        self._timer = self.set_interval(1 / 30, self._tick)
        self.start()

    # ----- lifecycle (the app can stop/start the visualizer with a keybind) -----

    def start(self) -> None:
        """Start CAVA and resume refreshing the spectrum."""
        if self._proc is not None and self._proc.poll() is not None:
            # CAVA died on its own, clean up before spawning a new one
            self._stop_cava()
        if self._proc is None:
            self._start_cava()
        # nothing to render without CAVA, so don't burn CPU on an idle timer
        if self._timer is not None and not self._cava_missing:
            self._timer.resume()

    def stop(self) -> None:
        """Stop CAVA and stop refreshing the spectrum (frees CPU while hidden)."""
        if self._timer is not None:
            self._timer.pause()
        self._stop_cava()
        self._latest = [0.0] * self._nbars
        self._smooth = [0.0] * self._nbars

    def _build_config(self) -> Path:
        fd, path = tempfile.mkstemp(prefix="pulse_cava_", suffix=".conf")
        with open(fd, "w") as f:
            f.write(
                "[general]\n"
                f"bars = {self._nbars}\n"
                "framerate = 30\n"
                "autosens = 1\n"
                "sensitivity = 120\n"
                "\n"
                "[input]\n"
                "method = pipewire\n"
                "source = auto\n"
                "sample_rate = 48000\n"
                "\n"
                "[output]\n"
                "method = raw\n"
                "raw_target = /dev/stdout\n"
                "data_format = binary\n"
                "bit_format = 16bit\n"
                "channels = mono\n"
                "\n"
                "[smoothing]\n"
                "monstercat = 1\n"
                "waves = 1\n"
            )
        return Path(path)

    def _start_cava(self) -> None:
        if shutil.which("cava") is None:
            # CAVA is optional (it lives in its own Nix package), so say so
            # once instead of failing silently behind a blank spectrum.
            self._cava_missing = True
            if not self._warned_about_cava:
                self._warned_about_cava = True
                self.notify(
                    "cava not found: visualizer disabled",
                    title="pulse",
                    severity="warning",
                )
            return
        try:
            self._cava_config = self._build_config()
            proc = subprocess.Popen(
                ["cava", "-p", str(self._cava_config)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._proc = None
            self._reader_thread = None
            return
        self._proc = proc
        self._reader_thread = threading.Thread(
            target=self._read_loop, args=(proc,), daemon=True
        )
        self._reader_thread.start()

    def _stop_cava(self) -> None:
        # don't block the UI: the reader thread exits once the pipe closes
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._proc = None
        self._reader_thread = None
        if self._cava_config is not None:
            try:
                self._cava_config.unlink()
            except Exception:
                pass
            self._cava_config = None

    def _read_loop(self, proc: subprocess.Popen) -> None:
        if proc.stdout is None:
            return
        bytes_per_frame = self._nbars * 2
        stream = proc.stdout
        raw = b""
        while True:
            chunk = stream.read(bytes_per_frame)
            if not chunk:
                break
            raw += chunk
            while len(raw) >= bytes_per_frame:
                frame_raw = raw[:bytes_per_frame]
                raw = raw[bytes_per_frame:]
                if proc is not self._proc:
                    # a newer CAVA instance took over, this data is stale
                    return
                vals = struct.unpack("<" + "H" * self._nbars, frame_raw)
                self._latest = [v / 65535.0 for v in vals]

    def _tick(self) -> None:
        # temporal smoothing for a more fluid, less jittery look
        attack = 0.6
        decay = 0.85
        latest = self._latest
        n = min(len(latest), len(self._smooth), self._nbars)
        for i in range(n):
            target = latest[i]
            cur = self._smooth[i]
            if target >= cur:
                self._smooth[i] += (target - cur) * attack
            else:
                self._smooth[i] += (target - cur) * decay
        self.refresh()

    def render(self) -> Text:
        if self._cava_missing:
            return Text(
                "  CAVA not found - visualizer disabled  ", style="italic #7a2020"
            )

        width = max(1, self.size.width)
        height = max(1, self.size.height)

        # fit bars into the available width with a 1-column gap between them
        max_bars = max(1, (width + 1) // 2)
        nbars = min(len(self._smooth), max_bars)

        bars = list(self._smooth[:nbars])

        # add a little energy kick so quiet music still shows some life
        peak = max(bars, default=0.0)
        if peak < 0.3:
            boost = (0.3 - peak) * 0.4
            bars = [min(1.0, v + boost) for v in bars]

        # center bars horizontally when there's leftover space
        used_cols = nbars * 2 - 1
        left = (width - used_cols) // 2

        text = Text()
        n_grad = len(GRADIENT)
        for r in range(height):
            col_index = 0
            for _c in range(left):
                text.append(" ")
            for i, v in enumerate(bars):
                from_bottom = height - 1 - r
                target = v * height
                full_rows = int(target)
                frac = (target - full_rows) * 8
                level = int(round(frac))

                if from_bottom < full_rows:
                    ch = BAR_CHARS[8]
                elif from_bottom == full_rows and 0 < target < height:
                    ch = BAR_CHARS[level] if level > 0 else " "
                else:
                    ch = " "

                if ch != " ":
                    frac_up = from_bottom / max(1, height - 1)
                    idx = min(n_grad - 1, int(frac_up * n_grad))
                    text.append(ch, style=GRADIENT[idx])
                else:
                    text.append(" ")
                col_index += 1
                if col_index < used_cols:
                    text.append(" ")
                    col_index += 1
            if r < height - 1:
                text.append("\n")
        return text

    def on_unmount(self) -> None:
        self.stop()


class BindBar(Widget):
    """A minimal, flat keybinding bar shown at the bottom of the UI."""

    DEFS = [
        ("space", "Play/Pause"),
        ("n/p", "Track"),
        ("←/→", "Seek"),
        ("↑/↓", "Volume"),
        ("s", "Library"),
        ("v", "Viz"),
        ("r", "Repeat"),
        ("m", "Mute"),
        ("q", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        for key, desc in self.DEFS:
            yield Static(f"{key} {desc}", classes="bind-item")

    def on_mount(self) -> None:
        self.screen.bindings_updated_signal.subscribe(self, self.bindings_changed)

    def bindings_changed(self, screen) -> None:
        self.call_after_refresh(self.recompose)


class PulsePlayer(App):
    CSS_PATH = "pulse.tcss"

    BINDINGS = [
        Binding("space", "toggle", "Play/Pause", priority=True),
        Binding("n", "next", "Next", priority=True),
        Binding("p", "prev", "Prev", priority=True),
        Binding("right", "seek_fwd", "+10s"),
        Binding("left", "seek_back", "-10s"),
        Binding("up", "vol_up", "Vol+"),
        Binding("down", "vol_down", "Vol-"),
        Binding("s", "toggle_library", "Library", priority=True),
        Binding("v", "toggle_visualizer", "Viz", priority=True),
        Binding("r", "repeat", "Repeat", priority=True),
        Binding("m", "mute", "Mute", priority=True),
        Binding("q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        music_files: list[str],
        backend: str = "mpv",
        mpd_client=None,
    ) -> None:
        super().__init__()
        if backend not in {"mpv", "mpd"}:
            raise ValueError("backend must be 'mpv' or 'mpd'")
        self.backend = backend
        self.music_files = music_files
        self._track_paths: list[str] = []
        self.current_index: int | None = None
        self.current_path: str | None = None
        self._pos: float | None = None
        self._duration: float | None = None
        self._volume: float | None = 100
        self._repeat_playlist = False
        self._muted = False
        self._pre_mute_volume: float | None = None
        self.player = None
        self.mpd_client = mpd_client
        self._mpd_status: dict = {}
        self._mpd_song: dict = {}
        self._mpd_queue_loaded = False
        self._mpd_error_notified = False
        self.register_theme(RED_THEME)
        self.theme = "pulse-red"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Horizontal(
            Vertical(
                Static("LIBRARY", id="lib-title"),
                Tree[str]("Library", id="lib-tree"),
                id="library-pane",
            ),
            Vertical(
                Static("Nothing Playing", id="now-title"),
                Visualizer("", id="viz"),
                id="now-pane",
            ),
            id="main-row",
        )
        yield Horizontal(
            Static("", id="bar-title", classes="oneline"),
            Static("--:-- / --:--", id="bar-time", classes="oneline"),
            id="header-row",
        )
        yield BindBar(id="bindbar")
        yield Horizontal(
            ProgressBar(total=100, show_percentage=False, show_eta=False, id="seek"),
            Static("--%", id="vol"),
            id="progress-row",
        )

    # ----- build library tree -----

    def _build_tree(self) -> None:
        tree = self.query_one("#lib-tree", Tree)
        tree.root.expand()
        self._track_paths = sorted(self.music_files)

        tracks = tree.root.add("Tracks", data=None, expand=True)
        album_root = tree.root.add("Albums", data=None, expand=True)

        for p in self._track_paths:
            tracks.add_leaf(Path(p).name, data=p)

        albums: dict[str, list[tuple[str, str]]] = {}
        for p in self._track_paths:
            name = Path(p).name
            folder = Path(p).parent.name or "Unknown Album"
            albums.setdefault(folder, []).append((name, p))

        for folder in sorted(albums):
            album_node = album_root.add(folder, data=None, expand=False)
            for name, p in sorted(albums[folder]):
                album_node.add_leaf(name, data=p)

    def on_mount(self) -> None:
        self._build_tree()
        self.query_one("#bar-title", Static).update(
            f" {len(self.music_files)} tracks loaded"
        )

        if self.backend == "mpv":
            try:
                mpv = load_mpv_module()
                self.player = mpv.MPV(
                    ytdl=False,
                    input_default_bindings=False,
                    input_vo_keyboard=False,
                    vo="null",
                    ao=resolve_audio_output(),
                    keep_open="yes",
                )
            except (RuntimeError, OSError) as exc:
                self.exit(message=f"pulse: {exc}")
                return

            self.player.loop = "no"
            self.player.loop_playlist = "no"
            self.player.observe_property("time-pos", self._on_time_pos)
            self.player.observe_property("duration", self._on_duration)
            self.player.observe_property("pause", self._on_pause)
            self.player.observe_property("volume", self._on_volume)
            self.player.observe_property("mute", self._on_mute)
            self.player.observe_property("eof-reached", self._on_eof)

            if self._track_paths:
                # Start paused: nothing plays until the user hits space.
                self._play(0, autoplay=False)
        else:
            if self.mpd_client is None:
                self.exit(message="pulse: MPD backend selected but no MPD client was supplied")
                return
            self._poll_mpd()
            self.set_interval(0.5, self._poll_mpd)

        self.set_focus(None)

    # ----- mpv callbacks (mpv event thread -> must hop to main thread) -----

    def _marshal(self, fn, *args):
        if not self.is_running or self.player is None:
            return
        self.call_from_thread(fn, *args)

    def _on_time_pos(self, _name, value):
        self._marshal(self._update_time_pos, value)

    def _on_duration(self, _name, value):
        self._marshal(self._update_duration, value)

    def _on_pause(self, _name, value):
        self._marshal(self._update_pause, value)

    def _on_volume(self, _name, value):
        self._marshal(self._update_volume, value)

    def _on_mute(self, _name, value):
        self._marshal(self._update_mute, value)

    def _on_eof(self, _name, value):
        self._marshal(self._on_eof_reached, value)

    def _poll_mpd(self) -> None:
        """Refresh the UI from MPD without making MPD a UI dependency."""
        if self.backend != "mpd" or self.mpd_client is None:
            return
        try:
            status = self.mpd_client.status()
            song = self.mpd_client.currentsong()
        except Exception as exc:
            self._mpd_status = {"state": "disconnected"}
            self._update_status()
            if not self._mpd_error_notified:
                self._mpd_error_notified = True
                self.notify(f"MPD connection lost: {exc}", severity="error")
            return

        self._mpd_error_notified = False
        self._mpd_status = status
        self._mpd_song = song
        path = song.get("file")
        if path:
            self.current_path = path
            try:
                self.current_index = self._track_paths.index(path)
            except ValueError:
                self.current_index = None

        try:
            self._pos = float(status.get("elapsed", "0") or 0)
        except (TypeError, ValueError):
            self._pos = 0.0
        duration = status.get("duration") or song.get("duration") or song.get("time")
        try:
            self._duration = float(str(duration).split(":")[-1] if ":" in str(duration) else duration)
        except (TypeError, ValueError):
            self._duration = None
        try:
            self._volume = float(status["volume"]) if int(status.get("volume", -1)) >= 0 else None
        except (TypeError, ValueError):
            self._volume = None

        self.query_one("#bar-time", Static).update(
            f" {fmt_time(self._pos)} / {fmt_time(self._duration)} "
        )
        if self._duration:
            self.query_one("#seek", ProgressBar).progress = (
                min(1.0, (self._pos or 0) / self._duration) * 100
            )
        else:
            self.query_one("#seek", ProgressBar).progress = 0
        self.query_one("#vol", Static).update(
            "MUTED" if self._muted else fmt_volume(self._volume)
        )
        self._update_status()

    # ----- UI updates (main thread only) -----

    def _update_time_pos(self, value):
        self._pos = value
        self.query_one("#bar-time", Static).update(
            f" {fmt_time(value)} / {fmt_time(self._duration)} "
        )
        if value is not None and self._duration:
            self.query_one("#seek", ProgressBar).progress = (
                min(1.0, value / self._duration) * 100
            )
        self._update_status()

    def _update_duration(self, value):
        self._duration = value
        self.query_one("#bar-time", Static).update(
            f" {fmt_time(self._pos)} / {fmt_time(value)} "
        )

    def _update_pause(self, value):
        self._update_status()

    def _update_volume(self, value):
        self._volume = value
        self.query_one("#vol", Static).update(f"{fmt_volume(value)}")

    def _update_mute(self, value):
        self.query_one("#vol", Static).update(
            f"{'MUTED' if value else fmt_volume(self._volume)}"
        )

    def _on_eof_reached(self, value):
        if not value:
            return
        self._pos = self._duration
        self.query_one("#seek", ProgressBar).progress = 100
        self.query_one("#bar-time", Static).update(
            f" {fmt_time(self._duration)} / {fmt_time(self._duration)} "
        )
        if not self._is_paused():
            self._play(self.current_index + 1 if self.current_index is not None else 0)

    def _update_status(self):
        state = self._state()
        title = self._title()
        self.query_one("#now-title", Static).update(f"{title}\n{state}")
        self.query_one("#bar-title", Static).update(f"{state}  {title}")
        header = self.query_one(Header)
        header.sub_title = f"{state}  {title}"

    # ----- helpers -----

    def _is_paused(self) -> bool:
        if self.backend == "mpd":
            return self._mpd_status.get("state") != "play"
        return bool(self.player.pause) if self.player else True

    def _state(self) -> str:
        if self.backend == "mpd":
            state = self._mpd_status.get("state", "stop")
            return {
                "play": "PLAYING",
                "pause": "PAUSED",
                "stop": "STOPPED",
                "disconnected": "MPD DISCONNECTED",
            }.get(state, "STOPPED")
        if self.player is None:
            return "STOPPED"
        return "PAUSED" if self.player.pause else "PLAYING"

    def _title(self) -> str:
        if self.backend == "mpd" and self._mpd_song:
            title = self._mpd_song.get("title")
            artist = self._mpd_song.get("artist")
            if title and artist:
                return f"{artist} - {title}"
            if title:
                return title
        if self.current_index is None:
            return "Nothing Playing"
        return Path(self.current_path or "").name or "Track"

    def _play(self, index: int, autoplay: bool = True) -> None:
        if not self._track_paths:
            return
        index %= len(self._track_paths)
        self.current_index = index
        self.current_path = self._track_paths[index]
        self._pos = 0.0
        self._duration = None
        self.query_one("#bar-title", Static).update(f" {self._title()} ")
        self.query_one("#seek", ProgressBar).progress = 0
        self.query_one("#bar-time", Static).update(" --:-- / --:-- ")

        if self.backend == "mpd":
            if self.mpd_client is None:
                return
            try:
                if not self._mpd_queue_loaded:
                    self.mpd_client.clear()
                    for path in self._track_paths:
                        self.mpd_client.add(path)
                    self._mpd_queue_loaded = True
                self.mpd_client.play(index)
                if not autoplay:
                    self.mpd_client.pause(1)
                self._poll_mpd()
            except Exception as exc:
                self.notify(f"MPD playback failed: {exc}", severity="error")
            return

        if self.player is None:
            return
        self.player.play(self.current_path)
        if not autoplay:
            # load the track without letting any audio out
            self.player.pause = True
        self._update_status()

    # ----- actions -----

    def action_toggle(self) -> None:
        if self.backend == "mpd":
            if self.mpd_client is None:
                return
            try:
                if self._mpd_status.get("state") == "play":
                    self.mpd_client.pause(1)
                elif self.current_index is None and self._track_paths:
                    self._play(0)
                    return
                else:
                    self.mpd_client.play()
                self._poll_mpd()
            except Exception as exc:
                self.notify(f"MPD playback failed: {exc}", severity="error")
        elif self.player is not None:
            self.player.pause = not self.player.pause

    def action_next(self) -> None:
        if self.backend == "mpd":
            if self.mpd_client is None:
                return
            if self.current_index is None and self._mpd_status.get("state") == "stop":
                self._play(0)
                return
            try:
                self.mpd_client.next()
                self._poll_mpd()
            except Exception as exc:
                self.notify(f"MPD next failed: {exc}", severity="error")
        elif self.current_index is not None:
            self._play(self.current_index + 1)

    def action_prev(self) -> None:
        if self.backend == "mpd":
            if self.mpd_client is None:
                return
            try:
                if (self._pos or 0) > 3:
                    self.mpd_client.seekcur("0")
                else:
                    self.mpd_client.previous()
                self._poll_mpd()
            except Exception as exc:
                self.notify(f"MPD previous failed: {exc}", severity="error")
            return
        if self.current_index is None or self.player is None:
            return
        if self.player.time_pos is not None and self.player.time_pos > 3:
            self.player.seek(0, "absolute")
        else:
            self._play(self.current_index - 1)

    def action_vol_up(self) -> None:
        if self.backend == "mpd":
            if self.mpd_client is not None:
                self.mpd_client.setvol(min(100, int(self._volume or 0) + 5))
                self._poll_mpd()
        elif self.player is not None:
            self.player.volume = min(130, (self.player.volume or 0) + 5)

    def action_vol_down(self) -> None:
        if self.backend == "mpd":
            if self.mpd_client is not None:
                self.mpd_client.setvol(max(0, int(self._volume or 0) - 5))
                self._poll_mpd()
        elif self.player is not None:
            self.player.volume = max(0, (self.player.volume or 0) - 5)

    def action_seek_fwd(self) -> None:
        if self.backend == "mpd":
            if self.mpd_client is not None:
                self.mpd_client.seekcur("+10")
                self._poll_mpd()
        elif self.player is not None:
            self.player.time_pos = (self.player.time_pos or 0) + 10

    def action_seek_back(self) -> None:
        if self.backend == "mpd":
            if self.mpd_client is not None:
                self.mpd_client.seekcur("-10")
                self._poll_mpd()
        elif self.player is not None:
            self.player.time_pos = max(0, (self.player.time_pos or 0) - 10)

    def action_repeat(self) -> None:
        self._repeat_playlist = not self._repeat_playlist
        if self.backend == "mpd" and self.mpd_client is not None:
            self.mpd_client.repeat(1 if self._repeat_playlist else 0)
            self._poll_mpd()
        elif self.player is not None:
            try:
                self.player.loop_playlist = "inf" if self._repeat_playlist else "no"
            except AttributeError:
                pass

    def action_mute(self) -> None:
        if self.backend == "mpd":
            if self.mpd_client is None:
                return
            if not self._muted:
                self._pre_mute_volume = self._volume
                self.mpd_client.setvol(0)
                self._muted = True
            else:
                self.mpd_client.setvol(int(self._pre_mute_volume or 50))
                self._muted = False
            self._poll_mpd()
        elif self.player is not None:
            self.player.mute = not self.player.mute

    def action_toggle_library(self) -> None:
        pane = self.query_one("#library-pane", Vertical)
        hidden = not pane.has_class("-hidden")
        pane.set_class(hidden, "-hidden")
        now = self.query_one("#now-pane", Vertical)
        # no-lib, not with-lib: the full-width rule belongs on the state where
        # the library is gone, otherwise now-pane overflows the row and clips
        # the right edge of the spectrum
        now.set_class(hidden, "no-lib")

    def action_toggle_visualizer(self) -> None:
        viz = self.query_one("#viz", Visualizer)
        hidden = not viz.has_class("-hidden")
        viz.set_class(hidden, "-hidden")
        # stop CAVA entirely while hidden so it doesn't burn CPU
        if hidden:
            viz.stop()
        else:
            viz.start()
        now = self.query_one("#now-pane", Vertical)
        now.set_class(hidden, "no-viz")

    # ----- event handlers -----

    @on(Tree.NodeSelected)
    def _on_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if data and self.player is not None:
            path = str(data)
            if path in self._track_paths:
                self._play(self._track_paths.index(path))

    def on_unmount(self) -> None:
        player = self.player
        self.player = None
        if player is not None:
            player.terminate()
        if self.mpd_client is not None:
            try:
                self.mpd_client.close()
            except Exception:
                pass
            try:
                self.mpd_client.disconnect()
            except Exception:
                pass


def main() -> None:
    args = parse_args()
    try:
        backend = selected_backend()
    except ValueError as exc:
        sys.exit(f"pulse: {exc}")

    mpd_client = None
    if backend == "mpd":
        try:
            mpd_client = connect_mpd()
            files = mpd_music_files(mpd_client)
        except Exception as exc:
            if mpd_client is not None:
                try:
                    mpd_client.disconnect()
                except Exception:
                    pass
            sys.exit(f"pulse: {exc}")
        if not files:
            try:
                mpd_client.disconnect()
            except Exception:
                pass
            sys.exit("pulse: MPD's music database has no supported audio files; update the MPD database first")
    else:
        files = scan_music(Path(args.dir))
        if not files:
            sys.exit(f"pulse: no audio files found under '{args.dir}'")

    PulsePlayer(files, backend=backend, mpd_client=mpd_client).run()


if __name__ == "__main__":
    main()
