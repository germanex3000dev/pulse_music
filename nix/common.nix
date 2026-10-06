# PULSE option declarations, shared by the NixOS and Home Manager modules.
#
# Both systems declare the same options and resolve the package the same way
# (see ./resolve.nix). The only difference is where installed packages are
# collected, which is why there are two thin entry points: ./module.nix and
# ./home-manager.nix.
{ lib, pkgs, ... }:

{
  options.programs.pulse = {
    enable = lib.mkEnableOption "PULSE, the terminal-native music player";

    # nullable so the module works without the flake or an overlay applied
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
