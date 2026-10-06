{
  description = "PULSE - a terminal-native music player with a heartbeat";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = fn: nixpkgs.lib.genAttrs systems (system: fn nixpkgs.legacyPackages.${system});
      forEachPackage = pkgs: rec {
        pulse = pkgs.callPackage ./nix/package.nix { };
        default = pulse;
      };
      pythonWithDeps =
        pkgs: python3:
        python3.withPackages (ps: [
          # nixpkgs renamed python-mpv to mpv (jaseg/python-mpv), support both
          (ps.python-mpv or ps.mpv)
          ps.rich
          ps.textual
        ]);
    in
    {
      overlays.default = final: _prev: {
        pulse = final.callPackage ./nix/package.nix { };
      };

      packages = forAllSystems forEachPackage;

      apps = forAllSystems (pkgs: rec {
        pulse = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.pulse}/bin/pulse";
          meta = {
            description = "Terminal-native TUI music player powered by Textual, mpv and CAVA";
            platforms = systems;
          };
        };
        default = pulse;
      });

      # Run it from a checkout: `nix develop` then `python main.py`
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          name = "pulse-dev";
          packages = [
            (pythonWithDeps pkgs pkgs.python3)
            pkgs.mpv
            pkgs.cava
          ];
          # the loader trick the package uses, so the venv-less dev shell
          # finds libmpv exactly like the packaged binary does
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.mpv ];
        };
      });

      checks = forAllSystems (
        pkgs:
        let
          source = import ./nix/source.nix { inherit (pkgs) lib; };
        in
        {
          # PULSE has to start up, find libmpv and mount its UI
          smoke =
            pkgs.runCommand "pulse-smoke-test"
              {
                nativeBuildInputs = [ (pythonWithDeps pkgs pkgs.python3) ];
                # the loader trick the package uses, minus CAVA and mpv: PULSE
                # has to survive without a visualizer
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
