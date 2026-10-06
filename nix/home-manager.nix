# PULSE module for Home Manager.
#
# Identical to the NixOS module in ./module.nix, except that Home Manager
# collects installed packages in `home.packages`.
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
    home.packages = [ pulsePackage ];
  };
}
