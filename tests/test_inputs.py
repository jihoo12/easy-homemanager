import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ehm import cli, inputs, ui
from ehm.context import Context
from ehm.nixfile import NixFile
from ehm.workspace import Session

FLAKE = """{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    noctalia.url = "github:noctalia-dev/noctalia/cachix";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
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

HOME = """{ config, pkgs, inputs, ... }:
{
  imports = [
    inputs.noctalia.homeModules.default
  ];
  home.packages = with pkgs; [
    bat
  ];
  programs.noctalia.enable = true;
}
"""

LOCK = {
    "nodes": {
        "root": {"inputs": {"nixpkgs": "nixpkgs", "noctalia": "noctalia", "home-manager": "home-manager"}},
        "nixpkgs": {"locked": {"rev": "aaaa1111", "lastModified": 1790000000, "type": "github"},
                    "original": {"type": "github", "owner": "nixos", "repo": "nixpkgs"}},
        "noctalia": {"inputs": {"nixpkgs": "nixpkgs_2"},
                     "locked": {"rev": "bbbb2222", "lastModified": 1791000000, "type": "github"},
                     "original": {"type": "github", "owner": "noctalia-dev", "repo": "noctalia", "ref": "cachix"}},
        "home-manager": {"inputs": {"nixpkgs": ["nixpkgs"]},
                         "locked": {"rev": "cccc3333", "lastModified": 1792000000, "type": "github"},
                         "original": {"type": "github", "owner": "nix-community", "repo": "home-manager"}},
    },
    "root": "root",
    "version": 7,
}

OFFER = inputs.Offer(["default", "cli"], {"homeManagerModules": ["default", "extra"]}, True)


class InputsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Path(tmp.name)
        (self.cfg / "flake.nix").write_text(FLAKE)
        (self.cfg / "home.nix").write_text(HOME)
        (self.cfg / "flake.lock").write_text(json.dumps(LOCK))
        self.ctx = Context(self.cfg, "nixos-flake", self.cfg / "home.nix", "u", "h")
        yes = mock.patch.object(ui, "ASSUME_YES", True)
        yes.start()
        self.addCleanup(yes.stop)

    def read(self, name):
        return (self.cfg / name).read_text()

    def save(self, change):
        session = Session(self.ctx)
        change(session)
        self.assertTrue(session.commit())

    def test_list_combines_declaration_lock_and_usage(self):
        items = {i.name: i for i in inputs.list_inputs(self.ctx, Session(self.ctx))}
        self.assertEqual(set(items), {"nixpkgs", "noctalia", "home-manager"})
        noctalia = items["noctalia"]
        self.assertEqual(noctalia.url, "github:noctalia-dev/noctalia/cachix")
        self.assertEqual(noctalia.rev, "bbbb2222")
        self.assertTrue(noctalia.own_nixpkgs)
        self.assertEqual(noctalia.used_by, ["home.nix: imports"])
        self.assertEqual(items["home-manager"].follows, {"nixpkgs": "nixpkgs"})
        self.assertFalse(items["home-manager"].own_nixpkgs)
        self.assertEqual(items["nixpkgs"].used_by, ["flake.nix: outputs"])

    def test_add_with_package_and_module(self):
        self.save(lambda s: inputs.plan_add(self.ctx, s, "nixvim", "https://github.com/nix-community/nixvim",
                                            package="default", module=("homeManagerModules", "default")))
        flake, home = self.read("flake.nix"), self.read("home.nix")
        self.assertIn('    nixvim = {\n      url = "github:nix-community/nixvim";\n'
                      '      inputs.nixpkgs.follows = "nixpkgs";\n    };', flake)
        self.assertIn("    inputs.noctalia.homeModules.default\n    inputs.nixvim.homeManagerModules.default\n  ];", home)
        self.assertIn("    bat\n    inputs.nixvim.packages.${pkgs.stdenv.hostPlatform.system}.default\n  ];", home)
        self.assertIsNone(NixFile(flake).shape_error)
        item = inputs.find_input(self.ctx, Session(self.ctx), "nixvim")
        self.assertEqual(sorted(item.used_by), ["home.nix: home.packages", "home.nix: imports"])
        self.assertIsNone(item.rev)  # not locked until the next build

    def test_add_creates_imports_as_a_plain_list_and_respects_no_follow(self):
        (self.cfg / "home.nix").write_text("{ config, pkgs, inputs, ... }:\n{\n  home.username = \"u\";\n}\n")
        self.save(lambda s: inputs.plan_add(self.ctx, s, "foo", "github:a/foo", module=("homeModules", "default"),
                                            follow_nixpkgs=False))
        self.assertIn("  imports = [\n    inputs.foo.homeModules.default\n  ];", self.read("home.nix"))
        self.assertNotIn("with pkgs; [\n    inputs.foo", self.read("home.nix"))
        self.assertIn('    foo.url = "github:a/foo";', self.read("flake.nix"))

    def test_add_names_a_new_input_in_a_closed_outputs_pattern(self):
        (self.cfg / "flake.nix").write_text(FLAKE.replace("{ self, nixpkgs, home-manager, ... }@inputs",
                                                          "{ self, nixpkgs, home-manager, noctalia }@inputs"))
        self.save(lambda s: inputs.plan_add(self.ctx, s, "foo", "github:a/foo", package="default"))
        self.assertIn("outputs = { self, nixpkgs, home-manager, noctalia, foo }@inputs:", self.read("flake.nix"))

    def test_add_rejects_duplicates_and_bad_names(self):
        with self.assertRaises(ui.EhmError):
            inputs.plan_add(self.ctx, Session(self.ctx), "noctalia", "github:x/y")
        with self.assertRaises(ui.EhmError):
            inputs.plan_add(self.ctx, Session(self.ctx), "bad name", "github:x/y")

    def test_remove_takes_the_input_and_its_entries(self):
        self.save(lambda s: inputs.plan_add(self.ctx, s, "foo", "github:a/foo", package="default",
                                            module=("homeModules", "default")))
        self.save(lambda s: inputs.plan_remove(self.ctx, s, "foo"))
        self.assertEqual(self.read("home.nix"), HOME)
        self.assertEqual(self.read("flake.nix"), FLAKE)

    def test_remove_drops_the_name_from_a_closed_outputs_pattern(self):
        (self.cfg / "flake.nix").write_text(FLAKE.replace("{ self, nixpkgs, home-manager, ... }@inputs",
                                                          "{ self, nixpkgs, noctalia, home-manager }@inputs"))
        (self.cfg / "home.nix").write_text(HOME.replace("  programs.noctalia.enable = true;\n", ""))
        self.save(lambda s: inputs.plan_remove(self.ctx, s, "noctalia"))
        self.assertIn("outputs = { self, nixpkgs, home-manager }@inputs:", self.read("flake.nix"))
        self.assertNotIn("noctalia", self.read("flake.nix"))
        self.assertNotIn("inputs.noctalia", self.read("home.nix"))

    def test_remove_refuses_uses_it_cannot_rewrite(self):
        for flake, home, extra, expected in [
            (FLAKE, HOME.replace("programs.noctalia.enable = true;",
                                 "programs.x.package = inputs.noctalia.packages.x86_64-linux.cli;"),
             None, "home.nix: programs.x.package"),
            (FLAKE, HOME, ("configuration.nix", "{ inputs, ... }: { imports = [ inputs.noctalia.nixosModules.default ]; }"),
             "configuration.nix"),
            (FLAKE.replace('inputs.nixpkgs.follows = "nixpkgs";', 'inputs.nixpkgs.follows = "noctalia";'), HOME, None,
             "inputs.home-manager.inputs.nixpkgs.follows"),
        ]:
            (self.cfg / "flake.nix").write_text(flake)
            (self.cfg / "home.nix").write_text(home)
            if extra:
                (self.cfg / extra[0]).write_text(extra[1])
            with self.assertRaises(ui.EhmError) as e:
                inputs.plan_remove(self.ctx, Session(self.ctx), "noctalia")
            self.assertIn(expected, str(e.exception))
            if extra:
                (self.cfg / extra[0]).unlink()
        with self.assertRaises(ui.EhmError) as e:  # the configuration itself depends on nixpkgs
            inputs.plan_remove(self.ctx, Session(self.ctx), "nixpkgs")
        self.assertIn("flake.nix: outputs", str(e.exception))

    def test_names_and_urls(self):
        self.assertEqual(inputs.normalize_url("https://github.com/nix-community/nixvim.git"), "github:nix-community/nixvim")
        self.assertEqual(inputs.normalize_url("https://gitlab.com/a/b/"), "gitlab:a/b")
        self.assertEqual(inputs.normalize_url("github:a/b/ref"), "github:a/b/ref")
        self.assertEqual(inputs.suggest_name("github:nix-community/NixVim"), "nixvim")
        self.assertEqual(inputs.suggest_name("git+https://example.com/x/my.flake.git?ref=main"), "my-flake")
        self.assertEqual(inputs.package_expr("my-flake", "default"),
                         "inputs.my-flake.packages.${pkgs.stdenv.hostPlatform.system}.default")

    def test_inspect_parses_nix_output(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(
            {"packages": ["default"], "homeModules": [], "homeManagerModules": ["default"], "hasNixpkgs": False}), "")
        with mock.patch("subprocess.run", return_value=result) as run:
            offer = inputs.inspect("https://github.com/a/b")
        self.assertIn('builtins.getFlake "github:a/b"', run.call_args[0][0][-1])
        self.assertEqual(offer, inputs.Offer(["default"], {"homeManagerModules": ["default"]}, False))

    def test_cli_add_picks_defaults_with_yes(self):
        args = cli.build_parser().parse_args(["-y", "inputs", "add", "github:a/tool"])
        with mock.patch.object(inputs, "inspect", return_value=OFFER), mock.patch.object(cli, "evaluate", return_value=None):
            args.func(self.ctx, args)
        home = self.read("home.nix")
        self.assertIn("inputs.tool.packages.${pkgs.stdenv.hostPlatform.system}.default", home)
        self.assertIn("inputs.tool.homeManagerModules.default", home)

    def test_cli_add_explicit_choices(self):
        args = cli.build_parser().parse_args(
            ["-y", "inputs", "add", "github:a/tool", "--name", "t", "--package", "cli", "--no-module"])
        with mock.patch.object(inputs, "inspect", return_value=OFFER), mock.patch.object(cli, "evaluate", return_value=None):
            args.func(self.ctx, args)
        home = self.read("home.nix")
        self.assertIn("inputs.t.packages.${pkgs.stdenv.hostPlatform.system}.cli", home)
        self.assertNotIn("inputs.t.homeManagerModules", home)
        args = cli.build_parser().parse_args(["-y", "inputs", "add", "github:a/x", "--package", "nope"])
        with mock.patch.object(inputs, "inspect", return_value=OFFER), self.assertRaises(ui.EhmError):
            args.func(self.ctx, args)


if __name__ == "__main__":
    unittest.main()
