{
  description = "ehm: Home Manager 를 쉽게 쓰게 해 주는 도구";

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
          checkPhase = ''
            runHook preCheck
            PYTHONPATH=src python -m unittest discover -s tests
            runHook postCheck
          '';
          meta.mainProgram = "ehm";
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
