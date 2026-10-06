# PULSE module, shared by NixOS and Home Manager.
#
# The only thing that differs between the two is where installed packages go:
# `environment.systemPackages` on NixOS, `home.packages` on Home Manager.
# That is what `packagesOption` selects - see ./home-manager.nix.
{
  config,
  lib,
  pkgs,
  packagesOption ? "environment.systemPackages",
}:

let
  cfg = config.programs.pulse;

  pulsePackage = cfg.package.override (old: {
    wrapperArgs =
      (old.wrapperArgs or [ ])
      ++ lib.optionals (cfg.musicDir != null) [
        "--set"
        "PULSE_MUSIC_DIR"
        (toString cfg.musicDir)
      ];
  });
in
{
  options.programs.pulse = {
    enable = lib.mkEnableOption "PULSE, the terminal-native music player";

    package = lib.mkPackageOption pkgs "pulse" { };

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

  config = lib.mkIf cfg.enable {
    ${packagesOption} = lib.mkDefault [ pulsePackage ];
  };
}
