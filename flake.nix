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
      forAllSystems =
        fn: nixpkgs.lib.genAttrs systems (system: fn nixpkgs.legacyPackages.${system});
      forEachPackage =
        pkgs: rec {
          pulse = pkgs.callPackage ./nix/package.nix { };
          default = pulse;
        };
      pythonWithDeps =
        pkgs: python3: python3.withPackages (
          ps: with ps; [
            python-mpv
            rich
            textual
          ]
        );
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

      checks = forAllSystems (pkgs: {
        # PULSE has to start up, find libmpv and mount its UI
        smoke = pkgs.runCommand "pulse-smoke-test"
          {
            name = "pulse-smoke-test";
            src = import ./nix/source.nix { inherit (pkgs) lib; };
            nativeBuildInputs = [ (pythonWithDeps pkgs pkgs.python3) ];
            LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.mpv ];
          }
          ''
            chmod -R +w .
            HOME="$TMPDIR" python -m unittest discover --start-directory tests
            touch "$out"
          '';
      });

      nixosModules = {
        pulse = import ./nix/module.nix { };
        default = self.nixosModules.pulse;
      };

      homeManagerModules = {
        pulse = import ./nix/home-manager.nix { };
        default = self.homeManagerModules.pulse;
      };

      formatter = forAllSystems (pkgs: pkgs.nixfmt-rfc-style);
    };
}
