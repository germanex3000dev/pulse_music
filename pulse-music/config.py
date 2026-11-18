"""PULSE configuration logic, includes user-defined colors, and a bit of the layout."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_path
from textual.color import Color, ColorParseError
from textual.theme import Theme

try:
    import tomllib  # only for py 3.11 and above
except ModuleNotFoundError:
    import tomli as tomllib

DEFAULT_COLORS: dict[str, str] = {
    # base - these also feed the Textual theme
    "background": "#000000",
    "surface": "#0a0a0a",
    "panel": "#0d0d0d",
    "foreground": "#ffcfcf",
    "primary": "#ff3333",
    "secondary": "#b31212",
    "accent": "#ff5555",
    "error": "#ff2222",
    # text
    "text": "#ff6b6b",
    "title": "#ff2a2a",
    "subtitle": "#ff8080",
    "label": "#ffb0b0",
    "muted": "#ff8a8a",
    "time": "#ff9b9b",
    "dim": "#5a2a2a",
    "notice": "#7a2020",
    "focus_text": "#ffffff",
    "progress": "#ff1a1a",
    "progress_text": "#ffdddd",
    # state backgrounds
    "highlight_bg": "#1c0a0a",
    "cursor_bg": "#330d0d",
    "selected_bg": "#471111",
    "track": "#1a1a1a",
    "scrollbar": "#c21818",
    "scrollbar_track": "#3a0a0a",
    "expander": "#ff4444",
}

#: the visualizer spectrum: index 0 (bottom, dim) -> N (top, bright)
DEFAULT_GRADIENT: list[str] = [
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

# The config key is a TCSS variable name in pulse.tcss
# The prefix "pulse-" is needed here, since Textual's built-in theme already claims
# bare names like "$background", "$text", and "$accent", and these theme values
# take precedence over the defaults of the application, which would just disable the config, silently...


def config_path() -> Path:
    """Here we resolve the config file locations, since $PULSE_CONFIG overrides."""
    env_path = os.environ.get("PULSE_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return user_config_path("pulse") / "config.toml"


@dataclass(frozen=True)
class Config:
    """The configuration is validated, with the defaults of course filled in."""

    colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    gradient: list[str] = field(default_factory=lambda: list(DEFAULT_GRADIENT))
    warnings: tuple[str, ...] = ()

    def css_variables(self) -> dict[str, str]:
        """The map configuration values to the TCSS variables that are used by pulse.tcss."""
        return {
            f"pulse-{key.replace('_', '-')}": value
            for key, value in self.colors.items()
        }

    def theme(self) -> Theme:
        """Build the PULSE Textual theme from the configured base colors."""
        colors = self.colors
        return Theme(
            name="pulse-red",
            primary=colors["primary"],
            secondary=colors["secondary"],
            accent=colors["accent"],
            # warning/success have always matched the accent in PULSE
            warning=colors["accent"],
            success=colors["accent"],
            error=colors["error"],
            foreground=colors["foreground"],
            background=colors["background"],
            surface=colors["surface"],
            panel=colors["panel"],
            dark=True,
        )


def _validate_color(key: str, value: object, warnings: list[str]) -> str | None:
    """Return a valid color string (or None), with a warning if it's not valid."""
    if not isinstance(value, str):
        warnings.append(
            f"colors.{key} must be a string, got {type(value).__name__}; "
            f"using defaults instead."
        )
        return None
    try:
        Color.parse(value)
    except ColorParseError:
        warnings.append(
            f"colors.{key} = {value!r} isn't a valid color; "
            f"using defaults instead."
        )
        return None
    return value


def _validate_gradient(value: object, warnings: list[str]) -> list[str] | None:
    """Return a valid gradient stop list (or None), with a warning if it isn't one."""
    if not isinstance(value, list):
        warnings.append(
            f"gradient.colors must be a list, got {type(value).__name__}; "
            f"using defaults instead."
        )
        return None
    if len(value) < 2:
        warnings.append(
            f"gradient.colors needs at least 2 stops, got {len(value)}; "
            f"using defaults instead."
        )
        return None
    validated: list[str] = []
    for index, stop in enumerate(value):
        if not isinstance(stop, str):
            warnings.append(
                f"gradient.colors[{index}] must be a string, got "
                f"{type(stop).__name__}; using defaults instead."
            )
            return None
        try:
            Color.parse(stop)
        except ColorParseError:
            warnings.append(
                f"gradient.colors[{index}] = {stop!r} isn't a valid color; "
                f"using defaults instead."
            )
            return None
        validated.append(stop)
    return validated


def load_config(path: Path | str | None = None) -> Config:
    """If you know how to read python, you know that this loads the TOML configuration.
    (and it also falls back to default if a problem occurs, somehow)
    The file missing obviously isn't an error, but the default theme is still used if it is, and
    it also won't complain about it.

    Any other thing such as bad TOML or a bad value produces a stderr warning, and falls back to the default.
    """
    cfg_path = Path(path) if path is not None else config_path()

    if not cfg_path.is_file():
        return Config()

    try:
        with open(cfg_path, "rb") as config_file:
            data = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        return Config(warnings=(f"{cfg_path} is invalid TOML: {error}",))
    except OSError as error:
        return Config(warnings=(f"cannot read {cfg_path}: {error}",))

    warnings: list[str] = []

    colors = dict(DEFAULT_COLORS)
    gradient = list(DEFAULT_GRADIENT)

    known_sections = {"colors", "gradient"}
    for section in data:
        if section not in known_sections:
            warnings.append(f"unknown section [{section}], ignoring.")

    raw_colors = data.get("colors", {})
    if not isinstance(raw_colors, dict):
        warnings.append("[colors] must be a table; using defaults instead.")
        raw_colors = {}

    for key, value in raw_colors.items():
        if key not in DEFAULT_COLORS:
            warnings.append(
                f"unknown color {key!r} "
                f"(valid: {', '.join(DEFAULT_COLORS)})"
            )
            continue
        validated = _validate_color(key, value, warnings)
        if validated is not None:
            colors[key] = validated

    raw_gradient = data.get("gradient", {})
    if not isinstance(raw_gradient, dict):
        warnings.append("[gradient] must be a table; using defaults instead.")
        raw_gradient = {}

    for key, value in raw_gradient.items():
        if key != "colors":
            warnings.append(f"unknown option gradient.{key} ignoring.")
            continue
        validated_gradient = _validate_gradient(value, warnings)
        if validated_gradient is not None:
            gradient = validated_gradient

    return Config(
        colors=colors,
        gradient=gradient,
        warnings=tuple(warnings),
    )
