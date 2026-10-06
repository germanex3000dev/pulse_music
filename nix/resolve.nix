# Resolve `programs.pulse` into a wrapped derivation.
#
# A plain function, not a module, so both entry points can call it:
#   import ./resolve.nix { inherit config lib pkgs; }
{
  config,
  lib,
  pkgs,
}:

let
  cfg = config.programs.pulse;

  # cfg.package is null when PULSE is not in nixpkgs and no overlay was
  # applied, so fall back to the bundled derivation
  base = if cfg.package != null then cfg.package else pkgs.callPackage ./package.nix { };
in
base.override (old: {
  # musicDir is the one option that has to reach the wrapped binary
  wrapperArgs =
    (old.wrapperArgs or [ ])
    ++ lib.optionals (cfg.musicDir != null) [
      "--set"
      "PULSE_MUSIC_DIR"
      (toString cfg.musicDir)
    ];
})
