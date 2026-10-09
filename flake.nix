{
  description = "PULSE - a terminal-native music player with a heartbeat";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = fn: nixpkgs.lib.genAttrs systems (system: fn nixpkgs.legacyPackages.${system});

      pulseFor = pkgs: pkgs.callPackage ./nix/package.nix { };

      # PULSE imports textual, rich and optional python-mpv / python-mpd2; the same set feeds the
      # package, the dev shell and the tests
      pythonWithDeps = pkgs: import ./nix/deps.nix { inherit (pkgs) python3; };
    in
    {
      overlays.default = final: _prev: {
        pulse = pulseFor final;
      };

      packages = forAllSystems (pkgs: {
        pulse = pulseFor pkgs;
        default = pulseFor pkgs;
      });

      apps = forAllSystems (
        pkgs:
        let
          app = {
            type = "app";
            program = "${pulseFor pkgs}/bin/pulse";
            meta = {
              description = "Terminal-native TUI music player with MPV and MPD backends, Textual and CAVA";
              platforms = systems;
            };
          };
        in
        {
          pulse = app;
          default = app;
        }
      );

      # `nix develop`, then `python main.py`; MPD mode uses PULSE_BACKEND=mpd
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          name = "pulse-dev";
          packages = [
            (pythonWithDeps pkgs)
            pkgs.mpv
            pkgs.cava
            pkgs.mpd
          ];
          # the loader trick from nix/package.nix, so the dev shell finds
          # libmpv exactly like the packaged binary does
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.mpv ];
        };
      });

      checks = forAllSystems (
        pkgs:
        let
          source = import ./nix/source.nix { inherit (pkgs) lib; };
        in
        {
          # PULSE has to start up, find libmpv, mount its UI and survive the
          # layout toggles. Deliberately no CAVA and no mpv on PATH.
          smoke =
            pkgs.runCommand "pulse-smoke-test"
              {
                nativeBuildInputs = [ (pythonWithDeps pkgs) ];
                LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.mpv ];
              }
              ''
                mkdir source
                cp -r ${source}/. source/
                chmod -R +w source
                cd source
                HOME="$TMPDIR" python tests/test_smoke.py
                touch "$out"
              '';
        }
      );

      nixosModules = {
        pulse = ./nix/module.nix;
        default = self.nixosModules.pulse;
      };

      homeManagerModules = {
        pulse = ./nix/home-manager.nix;
        default = self.homeManagerModules.pulse;
      };

      # nixfmt-tree only picks up *.nix, plain nixfmt chokes on the rest
      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);
    };
}
