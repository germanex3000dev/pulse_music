import argparse
import ctypes
import ctypes.util
import glob
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


_prepare_libmpv()

try:
    import mpv
except OSError as exc:  # no libmpv anywhere we know about
    sys.exit(
        f"pulse: {exc}\n"
        "hint: install mpv (on NixOS add `mpv` to environment.systemPackages)\n"
        "      or set PULSE_LIBMPV=/path/to/libmpv.so.2"
    )

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Header, Input, ProgressBar, Static, Tree

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pulse",
        description=(
            "A fully red, terminal-styled TUI music player "
            "powered by Textual and mpv."
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
        ("/", "Search"),
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
        # no priority flags: while the search field has focus every key must
        # reach it, so PULSE keys only apply when nothing editable is focused
        Binding("space", "toggle", "Play/Pause"),
        Binding("n", "next", "Next"),
        Binding("p", "prev", "Prev"),
        Binding("right", "seek_fwd", "+10s"),
        Binding("left", "seek_back", "-10s"),
        Binding("up", "vol_up", "Vol+"),
        Binding("down", "vol_down", "Vol-"),
        Binding("s", "toggle_library", "Library"),
        Binding("/", "search", "Search"),
        Binding("v", "toggle_visualizer", "Viz"),
        Binding("r", "repeat", "Repeat"),
        Binding("m", "mute", "Mute"),
        Binding("escape", "clear_search", "Clear"),
        Binding("q", "quit", "Quit", priority=True),
    ]

    def __init__(self, music_files: list[str]) -> None:
        super().__init__()
        self.music_files = music_files
        self._track_paths: list[str] = []
        self.current_index: int | None = None
        self.current_path: str | None = None
        self._pos: float | None = None
        self._duration: float | None = None
        self._volume: float | None = 100
        self._repeat_playlist = False
        self.player: mpv.MPV | None = None
        self.register_theme(RED_THEME)
        self.theme = "pulse-red"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Horizontal(
            Vertical(
                Input(
                    placeholder="search tracks...",
                    id="lib-search",
                    classes="-hidden",
                    # starts disabled so the hidden field is never auto-focused
                    # and never eats PULSE's single-letter keys
                    disabled=True,
                ),
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
        tree.clear()
        tree.root.expand()
        self._track_paths = sorted(self.music_files)

        query = self.query_one("#lib-search", Input).value.strip().lower()
        if query:
            terms = query.split()
            paths = [
                p
                for p in self._track_paths
                if all(t in Path(p).name.lower() for t in terms)
            ]
        else:
            paths = list(self._track_paths)

        tracks = tree.root.add("Tracks", data=None, expand=True)
        album_root = tree.root.add("Albums", data=None, expand=True)

        for p in paths:
            tracks.add_leaf(Path(p).name, data=p)

        albums: dict[str, list[tuple[str, str]]] = {}
        for p in paths:
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

        self.player = mpv.MPV(
            ytdl=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            vo="null",
            ao=resolve_audio_output(),
            keep_open="yes",
        )
        self.player.loop = "no"
        self.player.loop_playlist = "no"

        self.player.observe_property("time-pos", self._on_time_pos)
        self.player.observe_property("duration", self._on_duration)
        self.player.observe_property("pause", self._on_pause)
        self.player.observe_property("volume", self._on_volume)
        self.player.observe_property("mute", self._on_mute)
        self.player.observe_property("eof-reached", self._on_eof)

        if self._track_paths:
            # start paused: the library is loaded, but nothing plays until
            # the user hits space
            self._play(0, autoplay=False)

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
        return bool(self.player.pause) if self.player else True

    def _state(self) -> str:
        if self.player is None:
            return "STOPPED"
        return "PAUSED" if self.player.pause else "PLAYING"

    def _title(self) -> str:
        if self.current_index is None:
            return "Nothing Playing"
        return Path(self.current_path or "").name or "Track"

    def _play(self, index: int, autoplay: bool = True) -> None:
        if self.player is None or not self._track_paths:
            return
        index %= len(self._track_paths)
        self.current_index = index
        self.current_path = self._track_paths[index]
        self._pos = 0.0
        self._duration = None
        self.query_one("#bar-title", Static).update(f" {self._title()} ")
        self.query_one("#seek", ProgressBar).progress = 0
        self.query_one("#bar-time", Static).update(" --:-- / --:-- ")
        self.player.play(self.current_path)
        if not autoplay:
            # load the track without letting any audio out
            self.player.pause = True
        self._update_status()

    # ----- actions -----

    def action_toggle(self) -> None:
        if self.player is not None:
            self.player.pause = not self.player.pause

    def action_next(self) -> None:
        if self.current_index is not None:
            self._play(self.current_index + 1)

    def action_prev(self) -> None:
        if self.current_index is None:
            return
        if self.player.time_pos is not None and self.player.time_pos > 3:
            self.player.seek(0, "absolute")
        else:
            self._play(self.current_index - 1)

    def action_vol_up(self) -> None:
        if self.player is not None:
            self.player.volume = min(130, (self.player.volume or 0) + 5)

    def action_vol_down(self) -> None:
        if self.player is not None:
            self.player.volume = max(0, (self.player.volume or 0) - 5)

    def action_seek_fwd(self) -> None:
        if self.player is not None:
            self.player.time_pos = (self.player.time_pos or 0) + 10

    def action_seek_back(self) -> None:
        if self.player is not None:
            self.player.time_pos = max(0, (self.player.time_pos or 0) - 10)

    def action_repeat(self) -> None:
        if self.player is not None:
            self._repeat_playlist = not self._repeat_playlist
            try:
                self.player.loop_playlist = "inf" if self._repeat_playlist else "no"
            except AttributeError:
                pass

    def action_mute(self) -> None:
        if self.player is not None:
            self.player.mute = not self.player.mute

    def action_search(self) -> None:
        pane = self.query_one("#library-pane", Vertical)
        pane.remove_class("-hidden")
        search = self.query_one("#lib-search", Input)
        search.disabled = False
        search.remove_class("-hidden")
        search.focus()

    def _hide_search(self) -> None:
        """Take the search field back off screen and drop its query.

        Hiding it while keeping the filter would silently leave a library the
        user can no longer see the state of, so the field and the filter go
        away together.
        """
        search = self.query_one("#lib-search", Input)
        if search.has_focus:
            search.blur()
        self.set_focus(None)
        search.value = ""
        search.add_class("-hidden")
        search.disabled = True

    @on(Input.Changed)
    def _on_search_changed(self, event: Input.Changed) -> None:
        if event.input.id == "lib-search":
            self._build_tree()

    @on(Input.Submitted)
    def _on_search_submitted(self, event: Input.Submitted) -> None:
        """Enter plays the first match and puts the search field away."""
        if event.input.id != "lib-search":
            return
        tree = self.query_one("#lib-tree", Tree)
        tracks = next(
            (n for n in tree.root.children if str(n.label) == "Tracks"), None
        )
        if tracks is not None and tracks.children:
            node = tracks.children[0]
            self._play(self._track_paths.index(str(node.data)))
        self._hide_search()

    def action_clear_search(self) -> None:
        """Escape leaves the search field and closes it."""
        search = self.query_one("#lib-search", Input)
        if search.has_focus:
            self._hide_search()

    def action_toggle_library(self) -> None:
        pane = self.query_one("#library-pane", Vertical)
        hidden = not pane.has_class("-hidden")
        if hidden:
            # don't strand an open search field behind a hidden pane
            self._hide_search()
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


def main() -> None:
    args = parse_args()
    files = scan_music(Path(args.dir))
    if not files:
        sys.stderr.write(f"pulse: no audio files found under '{args.dir}'\n")
        sys.exit(1)
    PulsePlayer(files).run()


if __name__ == "__main__":
    main()
