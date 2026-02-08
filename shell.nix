{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  packages = with pkgs; [
    python312
    git
    sqlite
  ];

  shellHook = ''
    export UV_PYTHON_PREFERENCE=only-system

    # Ensure uv is available (installed into .nix-uv if not present)
    if [ ! -f .nix-uv/bin/uv ]; then
      echo "Installing uv..."
      python3 -m venv .nix-uv
      .nix-uv/bin/pip install --quiet uv
    fi
    export PATH="$PWD/.nix-uv/bin:$PATH"
  '';
}
