# PULSE module for Home Manager.
#
# Identical to the NixOS module except that Home Manager collects installed
# packages in `home.packages` instead of `environment.systemPackages`.
{
  ...
}:
{
  imports = [ ./module.nix ];

  _module.args = {
    pulsePackagesOption = "home.packages";
  };
}
