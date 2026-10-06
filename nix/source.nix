# The PULSE source, trimmed down to what actually belongs in the store.
#
# Keeps virtualenvs, build results and editor droppings out of derivations,
# which is what makes `nix build .` fast and reproducible.
{ lib }:

lib.cleanSourceWith {
  src = ../.;
  name = "pulse-source";
  filter =
    path: type:
    let
      name = baseNameOf (toString path);
    in
    !(lib.hasPrefix "." name)
    && name != "venv"
    && name != "result"
    && name != "__pycache__"
    # nothing outside of main.py + pulse.tcss is needed to run PULSE
    && !(type == "directory" && (name == "nix" || name == "assets"));
}
