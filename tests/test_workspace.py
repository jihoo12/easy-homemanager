import tempfile
import unittest
from pathlib import Path

from ehm import suggest, ui
from ehm.context import Context
from ehm.workspace import PACKAGES, Session

MAIN = """{ pkgs, ... }:
{
  imports = [ ./extra.nix ];
  programs.git.enable = true;
  programs.fish.enable = false;
}
"""

EXTRA = """{ pkgs, ... }:
{
  home.packages = with pkgs; [
    bat
    ripgrep
  ];
}
"""


class TestSession(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Path(tmp.name)
        (self.cfg / "home.nix").write_text(MAIN)
        (self.cfg / "extra.nix").write_text(EXTRA)
        self.session = Session(Context(self.cfg, "hm-channel", self.cfg / "home.nix", "u"))

    def test_follows_imports(self):
        self.assertEqual(self.session.package_names(), {"bat", "ripgrep"})

    def test_enabled_programs_only_counts_literal_true(self):
        self.assertEqual(self.session.enabled_programs(), {"git"})

    def test_remove_package_edits_the_module_that_lists_it(self):
        self.assertTrue(self.session.remove_package("bat"))
        self.assertEqual(self.session.changed_files(), [(self.cfg / "extra.nix").resolve()])
        self.assertEqual(self.session.package_names(), {"ripgrep"})
        self.assertFalse(self.session.remove_package("nope"))

    def test_file_with_packages(self):
        self.assertIsNotNone(self.session.file_with_packages().find(PACKAGES))


class TestSelection(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(ui.parse_selection("1 3", 5), [0, 2])
        self.assertEqual(ui.parse_selection("2-4, 1", 5), [0, 1, 2, 3])
        self.assertEqual(ui.parse_selection("a", 3), [0, 1, 2])

    def test_invalid(self):
        for answer in ("", "0", "6", "1-", "3-1", "x", "1 x"):
            self.assertIsNone(ui.parse_selection(answer, 5), answer)


class TestLevels(unittest.TestCase):
    def test_order_and_labels(self):
        levels = sorted([suggest.Level.INFO, suggest.Level.IMPORTANT, suggest.Level.OPTIONAL])
        self.assertEqual([lv.label for lv in levels], ["important", "optional", "info"])


if __name__ == "__main__":
    unittest.main()
