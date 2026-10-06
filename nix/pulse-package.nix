# Shared helper: turn `programs.pulse.musicDir` into the environment variable
# PULSE reads at startup. Called as `import ./pulse-package.nix { inherit config lib; }`.
{ config, lib }:

config.programs.pulse.package.override (old: {
  wrapperArgs =
    (old.wrapperArgs or [ ])
    ++ lib.optionals (config.programs.pulse.musicDir != null) [
      "--set"
      "PULSE_MUSIC_DIR"
      (toString config.programs.pulse.musicDir)
    ];
})
