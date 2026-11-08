import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from rich.console import Console
    from rich.text import Text
    from textual.widgets import DataTable, Input, SelectionList, Static

    from ehm import tui
except ImportError:  # Textual is an optional dependency.
    tui = None

from ehm import options, ui
from ehm.context import Context
from ehm.workspace import load_pending

HOME_NIX = """{ pkgs, ... }:
{
  home.packages = with pkgs; [
    bat
    ripgrep
  ];
}
"""


@unittest.skipIf(tui is None, "Textual is not installed")
class TuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.home, self.cfg = root / "home", root / "cfg"
        self.home.mkdir()
        self.cfg.mkdir()
        (self.cfg / "home.nix").write_text(HOME_NIX)
        env = {"HOME": str(self.home), "XDG_STATE_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(self.home / ".config")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = Context(self.cfg, "hm-channel", self.cfg / "home.nix", "u")

    def app(self):
        app = tui.EhmApp(self.ctx)
        redirect = ui.redirect(app.ui_sink)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        return app

    def home_nix(self):
        return (self.cfg / "home.nix").read_text()

    async def test_overview_and_packages(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            console = Console(record=True, width=120, file=io.StringIO())
            console.print(tui.overview_table(app.ctx, app.session))
            overview = console.export_text()
            self.assertIn("standalone Home Manager + channels", overview)
            self.assertRegex(overview, r"Packages\s+2")
            await pilot.press("3")
            table = app.query_one("#installed", DataTable)
            self.assertEqual(table.row_count, 2)

    async def test_remove_review_and_save(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            app.query_one("#installed", DataTable).focus()
            await pilot.press("x")
            self.assertTrue(app.session.has_changes())
            self.assertIn("1 unsaved", app.sub_title)
            self.assertIn("bat", self.home_nix())  # staged only
            await pilot.press("s")
            await pilot.click("#save")
            await pilot.pause()
            self.assertNotIn("bat", self.home_nix())
            self.assertIn("ripgrep", self.home_nix())
            self.assertFalse(app.session.has_changes())

    async def test_discard(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            app.query_one("#installed", DataTable).focus()
            await pilot.press("x", "s")
            await pilot.click("#discard")
            await pilot.pause()
            self.assertFalse(app.session.has_changes())
            self.assertEqual(self.home_nix(), HOME_NIX)

    async def test_adopt_dotfile_and_secret_refusal(self):
        (self.home / ".inputrc").write_text("set bell-style none\n")
        (self.home / ".leak").write_text("password=hunter2hunter2\n")
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("5")
            field = app.query_one("#path", Input)
            field.value = str(self.home / ".leak")
            await pilot.click("#add")
            self.assertFalse(app.session.has_changes())
            field.value = "~/.inputrc"
            field.focus()
            await pilot.press("enter")
            self.assertTrue(app.session.has_changes())
            await pilot.press("s")
            await pilot.click("#save")
            await pilot.pause()
            self.assertIn("mkOutOfStoreSymlink", self.home_nix())
            self.assertTrue((self.cfg / "dotfiles/.inputrc").exists())
            self.assertEqual(load_pending(self.ctx)[0]["action"], "adopt")
            self.assertEqual(app.query_one("#dotfile-table", DataTable).row_count, 1)

    async def test_suggestions_are_staged(self):
        (self.home / ".inputrc").write_text("x\n")
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            await app.workers.wait_for_complete()
            await pilot.pause()
            listing = app.query_one("#suggestion-list", SelectionList)
            self.assertEqual(listing.option_count, 1)
            listing.select_all()
            await pilot.click("#stage")
            self.assertTrue(app.session.has_changes())
            self.assertFalse(any(w.is_running for w in app.workers))  # no automatic re-analysis
            await pilot.press("s")
            await pilot.click("#save")
            await pilot.pause()
        # The adopted file must really be copied, not just referenced from home.nix.
        self.assertIn("mkOutOfStoreSymlink", self.home_nix())
        self.assertEqual((self.cfg / "dotfiles/.inputrc").read_text(), "x\n")

    async def test_generations_tab_shows_config_and_stages_restore(self):
        from ehm import generations

        profiles = Path(os.environ["XDG_STATE_HOME"]) / "nix/profiles"
        profiles.mkdir(parents=True)
        for n in (1, 2):
            store = self.home.parent / f"store/gen{n}-home-manager-generation"
            store.mkdir(parents=True)
            (profiles / f"home-manager-{n}-link").symlink_to(store)
            if n == 1:
                self.ctx.mode = "hm-flake"
                generations.record_snapshot(self.ctx, store)
                self.ctx.mode = "hm-channel"
        (profiles / "home-manager").symlink_to("home-manager-2-link")
        (self.cfg / "home.nix").write_text(HOME_NIX.replace("ripgrep", "jq"))
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("6")
            await pilot.pause()
            table = app.query_one("#gen-table", DataTable)
            self.assertEqual(table.row_count, 2)
            table.focus()
            await pilot.press("down")  # generation 1, which ehm recorded
            await pilot.pause()
            origin = app.query_one("#gen-origin", Static)
            self.assertIn("Recorded by ehm", str(origin.render()))
            pane = app.query_one(tui.GenerationsPane)
            comparisons = tui.generations.compare(app.ctx, tui.generations.config_of(app.ctx, pane._selected()))
            old_side = tui._side(comparisons, old=True).plain
            new_side = tui._side(comparisons, old=False).plain
            self.assertIn("ripgrep", old_side)  # the generation's version
            self.assertIn("jq", new_side)  # the current configuration
            self.assertEqual(len(old_side.splitlines()), len(new_side.splitlines()))
            self.assertEqual(app.query_one("#gen-new").border_title, "Current")
            # Scrolling either half moves the other (regression: the watcher crashed).
            for body in ("#gen-old-body", "#gen-new-body"):
                app.query_one(body, Static).update(Text("\n".join(map(str, range(300)))))
            await pilot.pause()
            new_pane, old_pane = app.query_one("#gen-new"), app.query_one("#gen-old")
            new_pane._scroll_down_for_pointer(animate=False)  # what the mouse wheel calls
            await pilot.pause()
            self.assertGreater(new_pane.scroll_y, 0)
            self.assertEqual(old_pane.scroll_y, new_pane.scroll_y)
            await pilot.press("t")
            self.assertTrue(app.query_one("#gen-diff").display)
            self.assertFalse(app.query_one("#gen-split").display)
            await pilot.press("w")
            self.assertTrue(app.session.has_changes())
            await pilot.press("s")
            await pilot.click("#save")
            await pilot.pause()
        self.assertEqual(self.home_nix(), HOME_NIX)

    async def test_split_view_scroll_sync_settles_mid_document(self):
        """Regression: the two halves used to echo each other and jitter in the middle."""
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("6")
            await pilot.pause()
            for body in ("#gen-old-body", "#gen-new-body"):
                app.query_one(body, Static).update(Text("\n".join(map(str, range(300)))))
            await pilot.pause()
            old_pane, new_pane = app.query_one("#gen-old"), app.query_one("#gen-new")
            app.scroll_sensitivity_y = 1.37  # fractional steps, as with smooth scrolling
            for _ in range(20):
                new_pane._scroll_down_for_pointer(animate=False)
                await pilot.pause(0.01)
            samples = []
            for _ in range(10):
                await pilot.pause(0.03)
                samples.append((round(old_pane.scroll_y), round(new_pane.scroll_y)))
        self.assertEqual(len(set(samples)), 1, samples)  # no movement once scrolling stops
        left, right = samples[0]
        self.assertEqual(left, right)
        self.assertGreater(right, 10)  # and scrolling was not fought back

    async def test_commit_dialog_commits_through_the_terminal_handoff(self):
        import subprocess

        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        os.environ.update(env)
        subprocess.run(["git", "-C", str(self.cfg), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.cfg), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.cfg), "commit", "-qm", "init"], check=True)
        (self.cfg / "home.nix").write_text(HOME_NIX.replace("bat", "jq"))
        app = self.app()
        ran = []

        async def fake_terminal(operation, **kw):  # headless tests cannot suspend the app
            ran.append(operation)
            operation()

        app.in_terminal = fake_terminal
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("m")
            self.assertIsInstance(app.screen, tui.CommitScreen)
            self.assertEqual(app.screen.query_one("#message", Input).value, "ehm: update home.nix")
            self.assertTrue(app.screen.query_one("#push").disabled)  # no remote
            app.screen.query_one("#message", Input).value = "from the tui"
            await pilot.click("#ok")
            await pilot.pause()
        self.assertEqual(len(ran), 1)
        log = subprocess.run(["git", "-C", str(self.cfg), "log", "-1", "--format=%s"], capture_output=True, text=True)
        self.assertEqual(log.stdout.strip(), "from the tui")

    async def test_quit_with_staged_changes_asks_first(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            app.query_one("#installed", DataTable).focus()
            await pilot.press("x", "q")
            self.assertIsInstance(app.screen, tui.ConfirmScreen)
            await pilot.click("#no")
            self.assertTrue(app.is_running)

    async def test_prompts_are_refused_while_redirected(self):
        app = self.app()
        async with app.run_test():
            with self.assertRaises(RuntimeError):
                ui.ask("anything")


@unittest.skipIf(tui is None, "Textual is not installed")
class EditOptionTest(unittest.IsolatedAsyncioTestCase):
    def option(self, kind, type_, values=None):
        return options.Option(("programs", "x", "opt"), type_, kind, "Some option.", values, "", {"value": None})

    async def run_dialog(self, option, interact):
        results = []

        class Host(tui.App):
            def on_mount(self):
                self.push_screen(tui.EditOptionScreen(option), results.append)

        app = Host()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await interact(app, pilot)
            await pilot.pause()
        return results

    async def test_bool(self):
        async def interact(app, pilot):
            await pilot.click("#false")

        self.assertEqual(await self.run_dialog(self.option("bool", "boolean"), interact), [(("programs", "x", "opt"), "false")])

    async def test_int_validation(self):
        async def interact(app, pilot):
            field = app.screen.query_one("#value", Input)
            field.value = "abc"
            await pilot.click("#ok")
            self.assertIn("integer", str(app.screen.query_one("#error", Static).render()))
            field.value = "42"
            await pilot.click("#ok")

        self.assertEqual(await self.run_dialog(self.option("int", "signed integer"), interact), [(("programs", "x", "opt"), "42")])

    async def test_cancel(self):
        async def interact(app, pilot):
            await pilot.click("#cancel")

        self.assertEqual(await self.run_dialog(self.option("str", "string"), interact), [None])


class ValueToExprTest(unittest.TestCase):
    def option(self, kind, type_, values=None):
        return options.Option(("p",), type_, kind, "", values, "", {})

    def test_kinds(self):
        self.assertEqual(options.value_to_expr(self.option("listOf", "list of string"), "a, b"), (("p",), '[ "a" "b" ]'))
        self.assertEqual(options.value_to_expr(self.option("attrsOf", "attribute set"), "1", "a.b"), (("p", "a", "b"), "1"))
        self.assertEqual(options.value_to_expr(self.option("enum", "one of", ["x", "y"]), "y"), (("p",), '"y"'))
        with self.assertRaises(ValueError):
            options.value_to_expr(self.option("int", "integer"), "1.5")


if __name__ == "__main__":
    unittest.main()
