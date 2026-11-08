import unittest
from pathlib import Path

from ehm.nixfile import (
    ModuleShapeError,
    NixFile,
    NixParseError,
    format_path,
    normalize_pkg,
    relative_nix_path,
    split_option_path,
    to_nix,
    tokenize,
)

PK = ("home", "packages")

SAMPLE = """{ config, pkgs, inputs, ... }:

{
  imports = [
    inputs.noctalia.homeModules.default
  ];
  home.username = "jihoo";
  programs.git = {
    enable = true;
    settings = {
      user = {
        name = "jihoo";
      };
    };
  };

  home.packages = with pkgs; [
    ripgrep
    fd            # used by mini.pick
    clang
    python3
  ];
  programs.noctalia = {
      enable = true;
  };
  xdg.configFile."niri/config.kdl".source = ./niri/config.kdl;

  programs.home-manager.enable = true;
}
"""


def items(nf, path=PK):
    b = nf.find(path)
    return [i.name for lst in nf.list_info(b) for i in lst.items]


class TestLexer(unittest.TestCase):
    def test_strings_comments_paths(self):
        src = r'''{ a = "x ${y "z}" } }"; /* } */ b = ''q ''${x} ${ "w" }''; c = ./a/b.nix; d = a // b; # }
        e = https://x.y/z; }'''
        kinds = [t for t in tokenize(src) if t.kind == "path"]
        self.assertEqual([t.text for t in kinds], ["./a/b.nix", "https://x.y/z"])
        nf = NixFile(src)
        self.assertIsNone(nf.shape_error)
        self.assertEqual([b.path[0] for b in nf.bindings], list("abcde"))

    def test_bad_brackets(self):
        with self.assertRaises(NixParseError):
            NixFile("{ a = [ 1 ; }")


class TestBindings(unittest.TestCase):
    def setUp(self):
        self.nf = NixFile(SAMPLE, Path("/x/home.nix"))

    def test_paths(self):
        self.assertIsNone(self.nf.shape_error)
        self.assertIsNotNone(self.nf.find(("programs", "git", "settings", "user", "name")))
        self.assertIsNotNone(self.nf.find(("xdg", "configFile", "niri/config.kdl", "source")))

    def test_let_and_with_wrapped_module(self):
        src = "{ pkgs, ... }:\nlet x = 1; y = { z = 2; };\nin with pkgs; {\n  home.packages = [ pkgs.a ];\n}\n"
        nf = NixFile(src)
        self.assertEqual(items(nf), ["pkgs.a"])
        self.assertIsNone(nf.find(("x",)))

    def test_unsupported_shape_is_reported_not_guessed(self):
        nf = NixFile("{ lib, ... }: lib.mkMerge [ { home.packages = []; } ]")
        self.assertIsNotNone(nf.shape_error)
        with self.assertRaises(ModuleShapeError):
            nf.set_expr(("a", "b"), "1")


class TestPackages(unittest.TestCase):
    def setUp(self):
        self.nf = NixFile(SAMPLE, Path("/x/home.nix"))

    def test_add_keeps_style_and_comments(self):
        out = self.nf.add_list_item(PK, "bat")
        self.assertIn("    python3\n    bat\n  ];", out.text)
        self.assertIn("# used by mini.pick", out.text)

    def test_remove_line_with_comment_only_touches_that_line(self):
        out = self.nf.remove_list_item(PK, "fd")
        self.assertNotIn("mini.pick", out.text)
        self.assertEqual(SAMPLE.replace("    fd            # used by mini.pick\n", ""), out.text)

    def test_remove_missing(self):
        with self.assertRaises(KeyError):
            self.nf.remove_list_item(PK, "nope")

    def test_inline_lists(self):
        nf = NixFile("{ pkgs, ... }: { home.packages = [ pkgs.a pkgs.b ]; }")
        self.assertIn("[ pkgs.a pkgs.b pkgs.c ]", nf.add_list_item(PK, "c").text)
        self.assertIn("[ pkgs.b ]", nf.remove_list_item(PK, "a").text)
        empty = NixFile("{ pkgs, ... }: { home.packages = with pkgs; [ ]; }")
        self.assertIn("[ c ]", empty.add_list_item(PK, "c").text)

    def test_concatenated_lists_and_complex_items(self):
        src = "{ pkgs, ... }: {\n  home.packages = with pkgs; [\n    a\n    (python3.withPackages (ps: [ ps.x ]))\n  ] ++ [ pkgs.z ];\n}\n"
        nf = NixFile(src)
        self.assertEqual(items(nf), ["a", "(python3.withPackages (ps: [ ps.x ]))", "pkgs.z"])
        self.assertNotIn("pkgs.z", nf.remove_list_item(PK, "z").text)
        with self.assertRaises(KeyError):
            nf.remove_list_item(PK, "x")

    def test_creates_list_and_adds_pkgs_argument(self):
        nf = NixFile("{ config, ... }:\n{\n  home.username = \"a\";\n}\n")
        out = nf.add_list_item(PK, "bat")
        self.assertIn("{ pkgs, config, ... }:", out.text)
        self.assertIn('  home.packages = with pkgs; [\n    bat\n  ];\n}', out.text)
        self.assertEqual(items(out), ["bat"])

    def test_nested_home_block(self):
        nf = NixFile("{ pkgs, ... }: {\n  home = {\n    packages = [\n      pkgs.a\n    ];\n  };\n}\n")
        self.assertIn("      pkgs.a\n      pkgs.b\n", nf.add_list_item(PK, "b").text)

    def test_normalize(self):
        self.assertEqual(normalize_pkg("pkgs.a"), "a")


class TestOptions(unittest.TestCase):
    def setUp(self):
        self.nf = NixFile(SAMPLE, Path("/x/home.nix"))

    def test_replace_existing(self):
        out = self.nf.set_expr(("programs", "git", "settings", "user", "name"), '"other"')
        self.assertIn('name = "other";', out.text)

    def test_insert_into_existing_block(self):
        out = self.nf.set_expr(("programs", "git", "settings", "user", "email"), '"a@b"')
        self.assertIn('        name = "jihoo";\n        email = "a@b";\n', out.text)

    def test_insert_nested_relative_to_block(self):
        out = self.nf.set_expr(("programs", "git", "lfs", "enable"), "true")
        self.assertIn("    lfs.enable = true;\n  };", out.text)

    def test_insert_at_root_and_next_to_sibling(self):
        out = self.nf.set_expr(("programs", "bat", "enable"), "true")
        self.assertIn("  programs.bat.enable = true;\n}", out.text)
        out = self.nf.set_expr(("xdg", "configFile", "a/b", "source"), "./x")
        self.assertIn(
            'xdg.configFile."niri/config.kdl".source = ./niri/config.kdl;\n  xdg.configFile."a/b".source = ./x;',
            out.text,
        )

    def test_opaque_parent_refused(self):
        nf = NixFile("{ lib, ... }: {\n  programs.git = lib.mkIf true { enable = true; };\n}\n")
        with self.assertRaises(ModuleShapeError):
            nf.set_expr(("programs", "git", "lfs"), "true")

    def test_unset(self):
        out = self.nf.unset(("programs", "noctalia"))
        self.assertNotIn("noctalia", out.text.replace("inputs.noctalia", ""))
        out = self.nf.unset(("xdg", "configFile", "niri/config.kdl"))
        self.assertNotIn("niri", out.text)
        self.assertIsNone(out.shape_error)
        with self.assertRaises(KeyError):
            self.nf.unset(("nope",))

    def test_unset_keeps_siblings(self):
        out = self.nf.unset(("programs", "git", "settings"))
        self.assertIn("    enable = true;\n  };", out.text)

    def test_ensure_arg(self):
        self.assertIs(self.nf.ensure_arg("config"), self.nf)
        nf = NixFile("{ pkgs, ... }: { }")
        self.assertIn("{ config, pkgs, ... }:", nf.ensure_arg("config").text)
        bare = NixFile("{ home.username = \"a\"; }")
        self.assertTrue(bare.ensure_arg("pkgs").text.startswith("{ pkgs, ... }:"))

    def test_string_defaults_in_args_not_confused(self):
        nf = NixFile('{ a ? { pkgs = 1; }, ... }: { b = 1; }')
        self.assertNotEqual(nf.ensure_arg("pkgs").text, nf.text)


class TestHelpers(unittest.TestCase):
    def test_to_nix(self):
        self.assertEqual(to_nix(True), "true")
        self.assertEqual(to_nix('a"${b}'), r'"a\"\${b}"')
        self.assertEqual(to_nix(["a", 1]), '[ "a" 1 ]')

    def test_paths(self):
        self.assertEqual(split_option_path('a.b."c.d".e'), ("a", "b", "c.d", "e"))
        self.assertEqual(format_path(("a", "c.d", "in")), 'a."c.d"."in"')
        with self.assertRaises(ModuleShapeError):
            split_option_path("a..b")

    def test_relative(self):
        base = Path("/e/n")
        self.assertEqual(relative_nix_path(Path("/e/n/dotfiles/.config/a"), base), "./dotfiles/.config/a")
        self.assertEqual(relative_nix_path(Path("/e/n/.zshrc"), base), "./.zshrc")
        self.assertEqual(relative_nix_path(Path("/e/x"), base), "../x")
        self.assertEqual(relative_nix_path(Path("/e/n/a b"), base), '(./. + "/a b")')


if __name__ == "__main__":
    unittest.main()
