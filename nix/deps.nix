# The Python environment PULSE runs in: the interpreter plus everything
# imported by main.py. Kept in one place so the package, the dev shell and
# the smoke test cannot drift apart.
{ python3 }:

python3.withPackages (ps: [
  # nixpkgs renamed python-mpv to mpv (jaseg/python-mpv), accept both
  (ps.python-mpv or ps.mpv)
  ps.rich
  ps.textual
])
