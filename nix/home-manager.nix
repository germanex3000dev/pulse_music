# PULSE module for Home Manager.
#
# Identical to the NixOS module in ./module.nix except that Home Manager
# collects installed packages in `home.packages`.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.pulse;
in
{
  imports = [ ./common.nix ];

  config = lib.mkIf cfg.enable {
    home.packages = [ (import ./resolve.nix { inherit config lib pkgs; }) ];
  };
}
