import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ehm import ui, vcs
from ehm.context import Context

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


class VcsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {**ENV, "HOME": str(root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.remote, self.cfg = root / "remote.git", root / "cfg"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.cfg)], check=True, capture_output=True)
        git(self.cfg, "checkout", "-q", "-b", "main")
        (self.cfg / "home.nix").write_text("{ }\n")
        (self.cfg / "old.nix").write_text("{ a = 1; }\n")
        git(self.cfg, "add", ".")
        git(self.cfg, "commit", "-q", "-m", "init")
        git(self.cfg, "push", "-q", "-u", "origin", "main")
        self.ctx = Context(self.cfg, "hm-flake", self.cfg / "home.nix", "u", "u")

    def test_status_includes_config_and_leaves_out_other_untracked_files(self):
        (self.cfg / "home.nix").write_text("{ x = 1; }\n")
        (self.cfg / "extra.nix").write_text("{ }\n")
        (self.cfg / "dotfiles/.config/app").mkdir(parents=True)
        (self.cfg / "dotfiles/.config/app/conf").write_text("x")
        (self.cfg / "notes.txt").write_text("token=...")
        git(self.cfg, "mv", "old.nix", "renamed.nix")
        st = vcs.status(self.ctx)
        self.assertEqual(sorted(st.to_commit), sorted([
            "home.nix", "extra.nix", "dotfiles/.config/app/conf", "renamed.nix",
        ]))
        self.assertEqual(sorted(st.to_stage), sorted(["home.nix", "extra.nix", "dotfiles/.config/app/conf"]))
        self.assertEqual(st.left_out, ["notes.txt"])
        self.assertTrue(st.has_remote)
        self.assertEqual(st.ahead, 0)
        self.assertEqual(vcs.default_message(self.ctx, st), "ehm: update extra.nix, home.nix, renamed.nix, 1 dotfile")

    def test_commit_and_push(self):
        (self.cfg / "home.nix").write_text("{ x = 1; }\n")
        (self.cfg / "notes.txt").write_text("secret")
        vcs.commit(self.ctx, "my message")
        self.assertEqual(git(self.cfg, "log", "-1", "--format=%s").strip(), "my message")
        self.assertEqual(git(self.cfg, "status", "--porcelain").strip(), "?? notes.txt")
        self.assertEqual(vcs.status(self.ctx).ahead, 1)
        vcs.push_changes(self.ctx)
        self.assertEqual(vcs.status(self.ctx).ahead, 0)
        self.assertEqual(git(self.remote, "log", "-1", "--format=%s").strip(), "my message")

    def test_commit_with_already_staged_rename_and_removal(self):
        """Regression: staged renames/removals made `git add` fail on paths that no longer exist."""
        (self.cfg / "gone.nix").write_text("{ }\n")
        git(self.cfg, "add", "gone.nix")
        git(self.cfg, "commit", "-qm", "add gone")
        git(self.cfg, "mv", "old.nix", "renamed.nix")  # staged rename
        git(self.cfg, "rm", "-q", "gone.nix")  # staged removal
        (self.cfg / "home.nix").write_text("{ y = 2; }\n")  # unstaged change
        vcs.commit(self.ctx, "move things")
        self.assertEqual(git(self.cfg, "status", "--porcelain").strip(), "")
        files = set(git(self.cfg, "ls-files").split())
        self.assertEqual(files, {"home.nix", "renamed.nix"})

    def test_commit_with_push_and_nothing_to_commit(self):
        vcs.commit(self.ctx, "unused", push=True)  # nothing to commit; push is a no-op
        self.assertEqual(git(self.cfg, "log", "--format=%s").split(), ["init"])

    def test_no_remote(self):
        git(self.cfg, "remote", "remove", "origin")
        self.assertFalse(vcs.status(self.ctx).has_remote)
        with self.assertRaises(ui.EhmError):
            vcs.push_changes(self.ctx)

    def test_not_a_repository(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ui.EhmError):
                vcs.status(Context(Path(d), "hm-flake", Path(d) / "home.nix", "u"))


if __name__ == "__main__":
    unittest.main()
