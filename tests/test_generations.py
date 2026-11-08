import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ehm import generations, ui
from ehm.context import Context
from ehm.workspace import Session

HOME_NIX = "{ pkgs, ... }:\n{\n  home.packages = with pkgs; [\n    bat\n  ];\n}\n"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.cfg = self.root / "cfg"
        self.cfg.mkdir()
        (self.cfg / "home.nix").write_text(HOME_NIX)
        env = {"HOME": str(self.root / "home"), "XDG_STATE_HOME": str(self.root / "state")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        yes = mock.patch.object(ui, "ASSUME_YES", True)
        yes.start()
        self.addCleanup(yes.stop)
        self.ctx = Context(self.cfg, "hm-flake", self.cfg / "home.nix", "u", "u")
        self.profiles = self.root / "state/nix/profiles"
        self.profiles.mkdir(parents=True)

    def make_generation(self, number, current=False, mtime=None):
        store = self.root / f"store/gen{number}-home-manager-generation"
        store.mkdir(parents=True)
        link = self.profiles / f"home-manager-{number}-link"
        link.symlink_to(store)
        if mtime is not None:
            os.utime(link, (mtime, mtime), follow_symlinks=False)
        if current:
            profile = self.profiles / "home-manager"
            profile.unlink(missing_ok=True)
            profile.symlink_to(link.name)
        return store


class TestListing(Base):
    def test_lists_newest_first_and_finds_previous(self):
        self.make_generation(1)
        self.make_generation(2)
        self.make_generation(3, current=True)
        gens = generations.list_generations(self.ctx)
        self.assertEqual([g.number for g in gens], [3, 2, 1])
        self.assertEqual([g.current for g in gens], [True, False, False])
        self.assertEqual(gens[1].home, (self.root / "store/gen2-home-manager-generation"))
        self.assertEqual(generations.find(self.ctx, None).number, 2)
        self.assertEqual(generations.find(self.ctx, 1).number, 1)
        with self.assertRaises(ui.EhmError):
            generations.find(self.ctx, 9)

    def test_nixos_maps_system_to_home_generation(self):
        system = self.root / "system-7-link"
        unit = system / "etc/systemd/system"
        unit.mkdir(parents=True)
        (unit / "home-manager-u.service").write_text(
            "[Service]\nExecStart=/nix/store/aaa-hm-setup-env /nix/store/bbb-home-manager-generation\nUser=u\n"
        )
        self.ctx.mode = "nixos-flake"
        self.assertEqual(generations._home_generation(self.ctx, system), Path("/nix/store/bbb-home-manager-generation"))

    def test_rollback_commands(self):
        store = self.make_generation(1, current=True)
        g = generations.list_generations(self.ctx)[0]
        self.assertEqual(generations.rollback_commands(self.ctx, g), [[str(self.profiles / "home-manager-1-link/activate")]])
        self.assertTrue(str(store).endswith("generation"))
        self.ctx.mode = "nixos-flake"
        g.number = 31
        cmds = generations.rollback_commands(self.ctx, g)
        self.assertEqual(cmds[0][-2:], ["--switch-generation", "31"])
        self.assertTrue(cmds[1][1].endswith("bin/switch-to-configuration"))


class TestSnapshots(Base):
    def test_recorded_snapshot_is_exact_and_restorable(self):
        store = self.make_generation(1, current=True)
        generations.record_snapshot(self.ctx, store)
        (self.cfg / "home.nix").write_text(HOME_NIX.replace("bat", "bat\n    fd"))
        g = generations.list_generations(self.ctx)[0]
        snapshot = generations.config_of(self.ctx, g)
        self.assertTrue(snapshot.exact)
        self.assertEqual(snapshot.files["home.nix"], HOME_NIX)
        diff = generations.diff_against_current(self.ctx, snapshot, "generation 1")
        self.assertIn("-    fd", diff)
        self.assertIn(f"{self.cfg / 'home.nix'} (generation 1)", diff[1])
        session = Session(self.ctx)
        generations.plan_restore(self.ctx, session, snapshot)
        self.assertTrue(session.commit())
        self.assertEqual((self.cfg / "home.nix").read_text(), HOME_NIX)

    def test_falls_back_to_last_commit_before_the_generation(self):
        def git(*args):
            subprocess.run(["git", "-C", str(self.cfg), *args], check=True, capture_output=True,
                           env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

        git("init", "-q")
        git("add", "home.nix")
        git("commit", "-q", "-m", "first", "--date=2020-01-01T00:00:00")
        subprocess.run(["git", "-C", str(self.cfg), "commit", "--amend", "-q", "--no-edit"],
                       env={**os.environ, "GIT_COMMITTER_DATE": "2020-01-01T00:00:00",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}, check=True)
        (self.cfg / "home.nix").write_text(HOME_NIX.replace("bat", "jq"))
        self.make_generation(1, current=True, mtime=time.mktime((2021, 1, 1, 0, 0, 0, 0, 0, -1)))
        snapshot = generations.config_of(self.ctx, generations.list_generations(self.ctx)[0])
        self.assertFalse(snapshot.exact)
        self.assertIn("first", snapshot.origin)
        self.assertEqual(snapshot.files["home.nix"], HOME_NIX)

    def test_unknown_without_snapshot_or_git(self):
        self.make_generation(1, current=True)
        self.assertIsNone(generations.config_of(self.ctx, generations.list_generations(self.ctx)[0]))


if __name__ == "__main__":
    unittest.main()
