# PULSE option declarations, shared by the NixOS and Home Manager modules.
#
# Both entry points need the same three options; the only thing that differs
# between the two systems is where installed packages are collected -
# `environment.systemPackages` on NixOS, `home.packages` on Home Manager.
{ lib, pkgs, ... }:

{
  options.programs.pulse = {
    enable = lib.mkEnableOption "PULSE, the terminal-native music player";

    # nullable so this works even when PULSE is not in nixpkgs and no overlay
    # is applied: nix/pulse-package.nix falls back to calling ./package.nix
    package = lib.mkPackageOption pkgs "pulse" {
      nullable = true;
      default = null;
    };

    musicDir = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/srv/music";
      description = ''
        Directory PULSE scans for audio files, recursively. When null, PULSE
        falls back to its own default of ~/Music, or to `$PULSE_MUSIC_DIR` if
        that is set in the environment.
      '';
    };
  };
}
