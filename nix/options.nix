# PULSE option declarations, shared by the NixOS and Home Manager modules.
#
# Both entry points need the same three options; the only thing that differs
# between the two systems is where installed packages are collected -
# `environment.systemPackages` on NixOS, `home.packages` on Home Manager.
{ lib, pkgs, ... }:

{
  options.programs.pulse = {
    enable = lib.mkEnableOption "PULSE, the terminal-native music player";

    # defaultPath so this works without the flake or an overlay applied: the
    # option resolves to pkgs.callPackage ./package.nix
    package = lib.mkPackageOption pkgs "pulse" { defaultPath = ./package.nix; };

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
