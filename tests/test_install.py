import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ehm import install, ui
from ehm.context import Context
from ehm.nixfile import NixFile
from ehm.workspace import Session

FLAKE = """{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager.url = "github:nix-community/home-manager";
  };

  outputs = { self, nixpkgs, home-manager, ... }@inputs: {
    nixosConfigurations.h = nixpkgs.lib.nixosSystem {
      modules = [
        home-manager.nixosModules.home-manager
        {
          home-manager.extraSpecialArgs = { inherit inputs; };
          home-manager.users.u = import ./home.nix;
        }
      ];
    };
  };
}
"""

HOME = "{ config, pkgs, ... }:\n{\n  home.packages = with pkgs; [\n    bat\n  ];\n}\n"


class InstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Path(tmp.name)
        (self.cfg / "home.nix").write_text(HOME)
        self.ctx = Context(self.cfg, "nixos-flake", self.cfg / "home.nix", "u", "h")
        which = mock.patch("shutil.which", return_value=None)
        which.start()
        self.addCleanup(which.stop)
        yes = mock.patch.object(ui, "ASSUME_YES", True)
        yes.start()
        self.addCleanup(yes.stop)

    def install(self, flake):
        (self.cfg / "flake.nix").write_text(flake)
        session = Session(self.ctx)
        self.assertFalse(install.is_installed(self.ctx, session))
        install.plan_install(self.ctx, session)
        self.assertTrue(session.commit())
        return (self.cfg / "flake.nix").read_text(), (self.cfg / "home.nix").read_text()

    def test_typical_nixos_flake(self):
        flake, home = self.install(FLAKE)
        self.assertIn('    ehm = {\n      url = "github:jihoo12/easy-homemanager";\n'
                      '      inputs.nixpkgs.follows = "nixpkgs";\n    };\n  };', flake)
        self.assertIn("{ inputs, config, pkgs, ... }:", home)
        self.assertIn("    bat\n    inputs.ehm.packages.${pkgs.stdenv.hostPlatform.system}.default\n  ];", home)
        self.assertIsNone(NixFile(flake).shape_error)
        self.assertTrue(install.is_installed(self.ctx, Session(self.ctx)))

    def test_closed_outputs_pattern_gets_the_new_input(self):
        flake, _ = self.install(FLAKE.replace("{ self, nixpkgs, home-manager, ... }@inputs", "{ self, nixpkgs, home-manager }@inputs"))
        self.assertIn("outputs = { self, nixpkgs, home-manager, ehm }@inputs:", flake)

    def test_top_level_input_style_and_custom_url(self):
        self.ctx.settings["ehm_flake_url"] = "git+ssh://git@github.com/u/ehm"
        flake = FLAKE.replace(
            '  inputs = {\n    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";\n'
            '    home-manager.url = "github:nix-community/home-manager";\n  };\n',
            '  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";\n'
            '  inputs.home-manager.url = "github:nix-community/home-manager";\n',
        )
        flake, _ = self.install(flake)
        self.assertIn('inputs.home-manager.url = "github:nix-community/home-manager";\n  inputs.ehm = {\n'
                      '    url = "git+ssh://git@github.com/u/ehm";', flake)

    def test_refuses_when_home_modules_do_not_get_inputs(self):
        (self.cfg / "flake.nix").write_text(FLAKE.replace("home-manager.extraSpecialArgs = { inherit inputs; };\n", ""))
        session = Session(self.ctx)
        self.assertIn("extraSpecialArgs", install.problem(self.ctx, session))
        with self.assertRaises(ui.EhmError):
            install.plan_install(self.ctx, session)

    def test_already_declared_inputs_argument_is_enough(self):
        (self.cfg / "home.nix").write_text(HOME.replace("{ config, pkgs, ... }", "{ config, pkgs, inputs, ... }"))
        flake = FLAKE.replace("home-manager.extraSpecialArgs = { inherit inputs; };\n", "")
        _, home = self.install(flake)
        self.assertTrue(home.startswith("{ config, pkgs, inputs, ... }:"))


if __name__ == "__main__":
    unittest.main()
