import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ehm import apply as applier
from ehm import dotfiles, ui
from ehm.context import Context, _detect_mode
from ehm.workspace import Session, load_pending

HOME_NIX = """{ config, pkgs, ... }:
{
  home.packages = with pkgs; [
    a
  ];
  home.file.".old".text = "x";
}
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.home = root / "home"
        self.cfg = root / "cfg"
        self.home.mkdir()
        self.cfg.mkdir()
        (self.cfg / "home.nix").write_text(HOME_NIX)
        env = {"HOME": str(self.home), "XDG_STATE_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(self.home / ".config")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        yes = mock.patch.object(ui, "ASSUME_YES", True)
        yes.start()
        self.addCleanup(yes.stop)
        self.ctx = Context(self.cfg, "hm-channel", self.cfg / "home.nix", "u")

    def session(self):
        return Session(self.ctx)


class TestSecrets(Base):
    def check(self, rel, content):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return dotfiles.find_secrets(path)

    def test_detects_and_does_not_leak_value(self):
        for rel, content in [
            (".leak", "export API_KEY=abcdef123456"),
            (".k", "-----BEGIN OPENSSH PRIVATE KEY-----"),
            (".t", "ghp_" + "a" * 30),
            (".ssh/config", "Host x"),
            (".config/gh/hosts.yml", "x"),
        ]:
            found = self.check(rel, content)
            self.assertTrue(found, rel)
            self.assertNotIn("abcdef", found[0][1])

    def test_ordinary_config_passes(self):
        self.assertEqual(self.check(".zshrc", "export EDITOR=nvim\nalias l=ls\nexport KEY=$OTHER\n"), [])


class TestAdopt(Base):
    def test_refuses_secret_outside_home_and_missing(self):
        (self.home / ".leak").write_text("password=hunter2hunter2")
        for raw in (str(self.home / ".leak"), "/etc/hostname", str(self.home / ".nope")):
            with self.assertRaises(ui.EhmError):
                dotfiles.plan_add(self.ctx, self.session(), raw)
        self.assertFalse((self.cfg / "dotfiles").exists())

    def test_declining_changes_nothing(self):
        (self.home / ".rc").write_text("x")
        s = self.session()
        dotfiles.plan_add(self.ctx, s, str(self.home / ".rc"))
        with mock.patch.object(ui, "ASSUME_YES", False), mock.patch("builtins.input", return_value="n"):
            self.assertFalse(s.commit())
        self.assertEqual((self.cfg / "home.nix").read_text(), HOME_NIX)
        self.assertFalse((self.cfg / "dotfiles").exists())
        self.assertEqual(load_pending(self.ctx), [])

    def test_link_default_adds_config_arg_and_keeps_original(self):
        (self.cfg / "home.nix").write_text("{ pkgs, ... }:\n{\n  home.username = \"u\";\n}\n")
        (self.home / ".rc").write_text("x")
        s = self.session()
        dotfiles.plan_add(self.ctx, s, str(self.home / ".rc"))
        self.assertTrue(s.commit())
        text = (self.cfg / "home.nix").read_text()
        self.assertIn("{ config, pkgs, ... }:", text)
        self.assertIn(f'mkOutOfStoreSymlink "{self.cfg}/dotfiles/.rc"', text)
        self.assertEqual((self.cfg / "dotfiles/.rc").read_text(), "x")
        self.assertEqual((self.home / ".rc").read_text(), "x")
        self.assertEqual(load_pending(self.ctx)[0]["action"], "adopt")

    def test_cancel_before_apply_keeps_original_and_cleans_repo(self):
        (self.home / ".rc").write_text("x")
        s = self.session()
        dotfiles.plan_add(self.ctx, s, str(self.home / ".rc"))
        s.commit()
        s = self.session()
        dotfiles.plan_remove(self.ctx, s, ".rc")
        s.commit()
        self.assertEqual((self.home / ".rc").read_text(), "x")
        self.assertFalse((self.cfg / "dotfiles").exists())
        self.assertEqual(load_pending(self.ctx), [])
        self.assertNotIn(".rc", (self.cfg / "home.nix").read_text())

    def test_inline_entry_needs_delete_flag(self):
        with self.assertRaises(ui.EhmError):
            dotfiles.plan_remove(self.ctx, self.session(), ".old")
        s = self.session()
        dotfiles.plan_remove(self.ctx, s, ".old", delete=True)
        self.assertNotIn(".old", s.load(self.ctx.home_file).text)

    def test_release_restores_without_overwriting(self):
        src = self.cfg / "dotfiles/.rc"
        src.parent.mkdir()
        src.write_text("repo")
        from ehm.workspace import save_pending

        save_pending(self.ctx, [{"action": "release", "target": str(self.home / ".rc"), "source": str(src), "keep_file": True, "discard_source": True}])
        dotfiles.process_pending_after_switch(self.ctx)
        self.assertEqual((self.home / ".rc").read_text(), "repo")
        self.assertFalse(src.exists())
        self.assertEqual(load_pending(self.ctx), [])


class TestConflicts(Base):
    def generation(self):
        gen = Path(self.tmp.name) / "gen/home-files"
        (gen / ".config/app").mkdir(parents=True)
        (gen / ".rc").write_text("x")
        (gen / ".config/app/a").write_text("x")
        outside = Path(self.tmp.name) / "linked"
        outside.mkdir()
        (gen / ".config/whole").symlink_to(outside)
        return gen.parent

    def test_leaves_treat_linked_dirs_as_one_item(self):
        self.assertEqual(
            applier.home_file_leaves(self.generation()),
            {".rc", ".config/app/a", ".config/whole"},
        )

    def test_only_unmanaged_files_conflict(self):
        gen = self.generation()
        (self.home / ".rc").write_text("mine")
        (self.home / ".config/whole/sub").mkdir(parents=True)
        (self.home / ".config/app").mkdir(parents=True)
        (self.home / ".config/app/a").symlink_to("/nix/store/abc-home-manager-files/.config/app/a")
        self.assertEqual(
            applier.find_conflicts(gen),
            [self.home / ".config/whole", self.home / ".rc"],
        )

    def test_move_and_restore_round_trip(self):
        (self.home / ".rc").write_text("mine")
        moved = applier.move_aside([self.home / ".rc"])
        self.assertFalse((self.home / ".rc").exists())
        self.assertEqual(moved[0][1].read_text(), "mine")
        applier.restore(moved)
        self.assertEqual((self.home / ".rc").read_text(), "mine")

    def test_failed_switch_restores_moved_files(self):
        gen = self.generation()
        self.ctx.mode = "hm-flake"
        self.ctx.host = "u"
        (self.home / ".rc").write_text("mine")
        with mock.patch.object(applier, "build_generation", return_value=gen), \
                mock.patch.object(applier, "current_generation", return_value=None), \
                mock.patch.object(applier, "switch_command", return_value=["false"]), \
                self.assertRaises(ui.EhmError):
            applier.apply(self.ctx, backup_conflicts=True)
        self.assertEqual((self.home / ".rc").read_text(), "mine")


class TestDetect(unittest.TestCase):
    def test_modes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertIsNone(_detect_mode(root))
            (root / "home.nix").write_text("{ }")
            self.assertEqual(_detect_mode(root), "hm-channel")
            (root / "flake.nix").write_text('{ outputs = x: { homeConfigurations."a" = 1; }; }')
            self.assertEqual(_detect_mode(root), "hm-flake")
            (root / "flake.nix").write_text("{ outputs = x: { nixosConfigurations.a = home-manager.nixosModules.home-manager; }; }")
            self.assertEqual(_detect_mode(root), "nixos-flake")
            # Commented-out text must not affect detection.
            (root / "flake.nix").write_text('{ # home-manager.users\n outputs = x: { homeConfigurations."a" = 1; }; }')
            self.assertEqual(_detect_mode(root), "hm-flake")


if __name__ == "__main__":
    unittest.main()
