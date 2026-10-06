# PULSE module for NixOS.
#
# Home Manager users want ./home-manager.nix instead - the only difference is
# that Home Manager collects packages in `home.packages`.
{
  config,
  lib,
  ...
}:

let
  cfg = config.programs.pulse;
  pulsePackage = import ./pulse-package.nix { inherit config lib; };
in
{
  imports = [ ./options.nix ];

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ pulsePackage ];
  };
}
