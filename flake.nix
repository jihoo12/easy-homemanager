{
  description = "ehm: a semi-automatic command-line interface for Home Manager";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in
    {
      packages = forAll (pkgs: {
        default = pkgs.python3Packages.buildPythonApplication {
          pname = "ehm";
          version = "0.1.0";
          pyproject = true;
          src = self;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = [ pkgs.python3Packages.textual ];
          checkPhase = ''
            runHook preCheck
            PYTHONPATH="src:$PYTHONPATH" python -m unittest discover -s tests
            runHook postCheck
          '';
          meta.mainProgram = "ehm";
        };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.textual ]))
            pkgs.ruff
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        };
      });

      apps = forAll (pkgs: {
        default = {
          type = "app";
          program = nixpkgs.lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };
      });
    };
}
