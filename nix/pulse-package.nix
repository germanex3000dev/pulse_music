# Shared helper: turn `programs.pulse.package` plus `musicDir` into the
# wrapped PULSE derivation.
#
# Called as:
#   import ./pulse-package.nix { inherit config lib pkgs; }
{ config, lib, pkgs }:

let
  cfg = config.programs.pulse;
  # fall back to the bundled package when no overlay put `pulse` in nixpkgs
  package = if cfg.package != null then cfg.package else pkgs.callPackage ./package.nix { };
in
package.override (old: {
  wrapperArgs =
    (old.wrapperArgs or [ ])
    ++ lib.optionals (cfg.musicDir != null) [
      "--set"
      "PULSE_MUSIC_DIR"
      (toString cfg.musicDir)
    ];
})
