# PULSE module for NixOS.
#
# Home Manager users want ./home-manager.nix instead: the only difference is
# that Home Manager collects packages in `home.packages`.
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
    environment.systemPackages = [ (import ./resolve.nix { inherit config lib pkgs; }) ];
  };
}
