# Package for PULSE, the terminal-native music player.
#
# Works with callPackage, with the flake (packages.default / packages.pulse),
# or through the NixOS and Home Manager modules in this directory.
#
# The interesting part is the wrapper. python-mpv loads libmpv through ctypes,
# which only knows about ldconfig and ld; on Nix both come up empty even though
# mpv is installed. So the wrapper puts mpv's libraries on the loader path and
# hands CAVA and mpv to PULSE through PATH.
{
  lib,
  stdenv,
  makeWrapper,
  python3,
  mpv,
  cava,
  wrapperArgs ? [ ],
}:

let
  pythonWithDeps = python3.withPackages (ps: [
    # nixpkgs renamed python-mpv to mpv (jaseg/python-mpv), support both
    (ps.python-mpv or ps.mpv)
    ps.rich
    ps.textual
  ]);
in
stdenv.mkDerivation {
  pname = "pulse";
  version = "1.2.0";

  src = import ./source.nix { inherit lib; };

  strictDeps = true;
  nativeBuildInputs = [ makeWrapper ];

  dontConfigure = true;
  dontBuild = true;

  # Textual resolves CSS_PATH relative to main.py, so the two files just have
  # to end up in the same directory - no need to wrap --cd.
  installPhase = ''
    runHook preInstall

    install -Dm644 main.py "$out/libexec/pulse/main.py"
    install -Dm644 pulse.tcss "$out/libexec/pulse/pulse.tcss"

    makeWrapper ${pythonWithDeps}/bin/python3 "$out/bin/pulse" \
      --add-flags "$out/libexec/pulse/main.py" \
      --prefix LD_LIBRARY_PATH : ${lib.makeLibraryPath [ mpv ]} \
      --prefix PATH : ${
        lib.makeBinPath [
          mpv
          cava
        ]
      } \
      ${lib.escapeShellArgs wrapperArgs}

    runHook postInstall
  '';

  meta = {
    description = "Terminal-native TUI music player powered by Textual, mpv and CAVA";
    longDescription = ''
      A lightweight, keyboard-driven TUI music player. Local music is played by
      mpv, the interface is built with Textual, and the spectrum visualizer
      reads raw bar data from CAVA.
    '';
    license = lib.licenses.mit;
    mainProgram = "pulse";
    platforms = lib.platforms.linux;
  };
}
