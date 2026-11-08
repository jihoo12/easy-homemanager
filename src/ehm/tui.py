"""Full-screen terminal UI built on Textual.

The TUI keeps one staging :class:`~ehm.workspace.Session` for the whole run. Every
action (adding a package, setting an option, adopting a dotfile, applying a
suggestion) only stages a change; the review screen (``s``) shows the combined
diff and writes everything at once. Operations that need a real terminal, such
as ``sudo nixos-rebuild`` or streaming build logs, temporarily suspend the UI.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Select,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option as ListOption
from textual.widgets.selection_list import Selection

from . import __version__, dotfiles, options, suggest, ui
from . import apply as applier
from .context import MODE_LABELS, Context
from .nixfile import ModuleShapeError, format_path
from .workspace import (
    PACKAGES,
    Session,
    evaluate,
    git,
    is_git_repo,
    load_pending,
    untracked_files,
)

EDITABLE_ERRORS = (ui.EhmError, ModuleShapeError)


# ---- dialogs ------------------------------------------------------------
class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, message: str, yes: str = "Yes", no: str = "Cancel", danger: bool = False):
        super().__init__()
        self.message, self.yes, self.no, self.danger = message, yes, no, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.message))
            with Horizontal(classes="buttons"):
                yield Button(self.yes, id="yes", variant="error" if self.danger else "primary")
                yield Button(self.no, id="no")

    @on(Button.Pressed)
    def _answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class EditOptionScreen(ModalScreen[tuple[tuple[str, ...], str] | None]):
    """Edit one option. Dismisses with ``(path, nix_expression)`` or ``None``."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, option: options.Option):
        super().__init__()
        self.option = option
        self.kind = options.input_kind(option)

    def compose(self) -> ComposeResult:
        o = self.option
        with Vertical(classes="dialog wide"):
            yield Label(Text(format_path(o.path), style="bold"))
            if o.description:
                yield Static(Text(o.description.strip()[:600]), classes="muted")
            details = f"Type: {o.type}\nCurrent: {o.current_text}"
            if o.example:
                details += f"\nExample: {' '.join(o.example.split())[:200]}"
            yield Static(Text(details))
            if self.kind == "bool":
                with Horizontal(classes="buttons"):
                    yield Button("true", id="true", variant="primary")
                    yield Button("false", id="false")
                    yield Button("Cancel", id="cancel")
                return
            if self.kind == "enum":
                labels = [(json.dumps(v, ensure_ascii=False), i) for i, v in enumerate(o.values)]
                yield Select(labels, allow_blank=False, id="value")
            elif self.kind == "freeform":
                yield Input(placeholder="Sub-key, e.g. user.name", id="subkey")
                yield Input(placeholder="Value: true/false, a number or a string", id="value")
            else:
                placeholder = {
                    "list": "Comma-separated values",
                    "string": "Value",
                    "int": "Integer",
                    "nix": "Raw Nix expression",
                }[self.kind]
                yield Input(placeholder=placeholder, id="value")
            yield Static("", id="error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Set", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "cancel":
            self.dismiss(None)
        elif button in ("true", "false"):
            self.dismiss(options.value_to_expr(self.option, button == "true"))
        else:
            self._submit()

    @on(Input.Submitted)
    def _submit(self) -> None:
        widget = self.query_one("#value")
        try:
            if self.kind == "enum":
                result = options.value_to_expr(self.option, self.option.values[widget.value])
            else:
                subkey = self.query_one("#subkey", Input).value if self.kind == "freeform" else ""
                if not widget.value:
                    raise ValueError("a value is required")
                result = options.value_to_expr(self.option, widget.value, subkey)
        except (ValueError, ModuleShapeError) as e:
            self.query_one("#error", Static).update(f"Invalid value: {e}")
            return
        self.dismiss(result)


class ReleaseScreen(ModalScreen[str | None]):
    """Ask how to stop managing a dotfile. Dismisses with ``restore``, ``delete`` or ``None``."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, entry: dotfiles.Entry):
        super().__init__()
        self.entry = entry

    def compose(self) -> ComposeResult:
        can_restore = not self.entry.inline and self.entry.source is not None
        with Vertical(classes="dialog"):
            yield Static(Text(f"Stop managing {ui.tilde(self.entry.target)}?", style="bold"))
            if not can_restore:
                yield Static("There is no source file to restore, so it can only be removed.", classes="muted")
            with Horizontal(classes="buttons"):
                yield Button("Restore as regular file", id="restore", variant="primary", disabled=not can_restore)
                yield Button("Remove", id="delete", variant="error")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "cancel" else event.button.id)


class ReviewScreen(ModalScreen[str | None]):
    """Show the staged diff. Dismisses with ``save``, ``discard`` or ``None``."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Back")]

    def __init__(self, session: Session):
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog full"):
            yield Label(Text("Staged changes", style="bold"))
            with VerticalScroll(id="diff"):
                for _, lines in self.session.diffs():
                    yield Static(Syntax("\n".join(lines), "diff", theme="ansi_dark", word_wrap=True))
                for note in self.session.notes:
                    yield Static(Text(f"• {note}"))
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Discard all", id="discard", variant="error")
                yield Button("Back", id="back")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "back" else event.button.id)


# ---- tabs -----------------------------------------------------------------
class Pane(Vertical):
    """Base class for tab contents; ``refresh_data`` re-reads the staging session."""

    @property
    def ehm(self) -> EhmApp:
        return self.app  # type: ignore[return-value]

    def refresh_data(self) -> None:
        pass

    def activated(self) -> None:
        pass


def overview_table(ctx: Context, session: Session) -> Table:
    """The status summary shown on the Overview tab."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()

    def section(title: str) -> None:
        table.add_row(Text(title, style="bold underline"), "")

    section("Configuration")
    table.add_row("Mode", MODE_LABELS[ctx.mode])
    table.add_row("Directory", ui.tilde(ctx.config_dir))
    table.add_row("Home file", ui.tilde(ctx.home_file))
    table.add_row("User", ctx.user + (f"  (target {ctx.host})" if ctx.host else ""))
    files = session.module_files()
    if len(files) > 1:
        table.add_row("Modules", "\n".join(ui.tilde(f) for f in files[1:]))
    table.add_row("", "")
    section("Managed")
    table.add_row("Packages", str(len(session.packages())))
    table.add_row("Programs", str(len(session.enabled_programs())))
    table.add_row("Dotfiles", str(len(dotfiles.entries(session))))
    pending = load_pending(ctx)
    if pending:
        table.add_row("", "")
        section("Pending apply")
        for p in pending:
            table.add_row(p["action"], ui.tilde(p["target"]))
    table.add_row("", "")
    section("Git")
    if is_git_repo(ctx):
        changed = [line for line in git(ctx, "status", "--short").stdout.splitlines() if line]
        table.add_row("Uncommitted", str(len(changed)))
        table.add_row("Untracked", str(len(untracked_files(ctx))))
    else:
        table.add_row("", Text("Not a git repository; `git init` is recommended.", style="yellow"))
    problems = [nf for nf in session.modules() if nf.shape_error]
    if problems:
        table.add_row("", "")
        section("Cannot edit automatically")
        for nf in problems:
            table.add_row(ui.tilde(nf.path), Text(nf.shape_error or "", style="red"))
    return table


class OverviewPane(Pane):
    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(id="overview-body")

    def refresh_data(self) -> None:
        self.query_one("#overview-body", Static).update(overview_table(self.ehm.ctx, self.ehm.session))


class SuggestionsPane(Pane):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.fixable: list[suggest.Suggestion] = []
        self.analyzed = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Button("Analyze", id="analyze")
            yield Button("Stage selected", id="stage", variant="primary", disabled=True)
        yield SelectionList[int](id="suggestion-list")
        yield Static("", id="why", classes="help")
        yield Static("", id="notes", classes="muted")

    def activated(self) -> None:
        if not self.analyzed:
            self.analyze()

    @on(Button.Pressed, "#analyze")
    def analyze(self) -> None:
        self.analyzed = True
        self.query_one("#suggestion-list").loading = True
        self._analyze()

    @work(thread=True, exclusive=True, group="suggest")
    def _analyze(self) -> None:
        try:
            items = suggest.analyze(self.ehm.ctx, self.ehm.session)
        except EDITABLE_ERRORS as e:
            self.app.call_from_thread(self.ehm.report_error, e)
            items = []
        self.app.call_from_thread(self._show, items)

    def _show(self, items: list[suggest.Suggestion]) -> None:
        listing = self.query_one("#suggestion-list", SelectionList)
        listing.loading = False
        listing.clear_options()
        self.fixable = [s for s in items if s.fix]
        for i, s in enumerate(self.fixable):
            prompt = Text.assemble((f"[{s.level.label}] ", _level_style(s.level)), s.title)
            listing.add_option(Selection(prompt, i))
        notes = [s for s in items if not s.fix]
        text = Text()
        for s in notes:
            text.append(f"[{s.level.label}] ", _level_style(s.level))
            text.append(f"{s.title}\n", style="bold")
            text.append(f"    {s.why}\n")
        if not items:
            text = Text("No suggestions. Your configuration looks tidy.", style="green")
        self.query_one("#notes", Static).update(text)
        self.query_one("#why", Static).update("")
        self.query_one("#stage", Button).disabled = not self.fixable

    @on(SelectionList.SelectionHighlighted)
    def _highlighted(self, event: SelectionList.SelectionHighlighted) -> None:
        s = self.fixable[event.selection.value]
        self.query_one("#why", Static).update(Text(s.why))

    @on(Button.Pressed, "#stage")
    def action_stage(self) -> None:
        listing = self.query_one("#suggestion-list", SelectionList)
        chosen = [self.fixable[i] for i in listing.selected]
        if not chosen:
            self.app.notify("Select suggestions with Space first.", severity="warning")
            return

        def fix_all(session: Session) -> None:
            for s in chosen:
                try:
                    s.fix(session)
                except ui.EhmError as e:
                    ui.warn(f"Skipped '{s.title}': {e}")

        if self.ehm.stage(fix_all, f"Staged {len(chosen)} suggestion(s)"):
            # Don't re-analyze automatically: that starts a background `nix eval`, which
            # could still be running when the user applies (see EhmApp.in_terminal).
            self._show([])
            self.query_one("#notes", Static).update(
                Text("Staged. Press Analyze to refresh the list.", style="yellow")
            )


class PackagesPane(Pane):
    BINDINGS = [Binding("x,delete", "remove", "Remove")]

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(classes="column"):
                yield Label(Text("Installed", style="bold"))
                yield DataTable(id="installed", cursor_type="row")
            with Vertical(classes="column"):
                yield Input(placeholder="Search nixpkgs and press Enter", id="search")
                yield DataTable(id="results", cursor_type="row")
                yield Static("Enter on a result stages it for installation.", classes="muted")

    def on_mount(self) -> None:
        installed = self.query_one("#installed", DataTable)
        installed.add_column("Name", key="name")
        installed.add_column("Via", key="via")
        installed.add_column("File", key="file")
        results = self.query_one("#results", DataTable)
        results.add_column("Package", key="name")
        results.add_column("Version", key="version")
        results.add_column("Description", key="description")

    def refresh_data(self) -> None:
        session = self.ehm.session
        table = self.query_one("#installed", DataTable)
        table.clear()
        for nf, name, simple in session.packages():
            label = Text(name) if simple else Text(name, style="dim")
            table.add_row(label, "home.packages", ui.tilde(nf.path), key=f"pkg:{name}" if simple else None)
        for name in sorted(session.enabled_programs()):
            table.add_row(name, f"programs.{name}", "", key=f"prog:{name}")

    @on(Input.Submitted, "#search")
    def _search(self, event: Input.Submitted) -> None:
        terms = event.value.split()
        if terms:
            self.query_one("#results").loading = True
            self._run_search(terms)

    @work(thread=True, exclusive=True, group="search")
    def _run_search(self, terms: list[str]) -> None:
        try:
            rows = self.ehm.ctx.search_packages(terms)
        except ui.EhmError as e:
            self.app.call_from_thread(self.ehm.report_error, e)
            rows = []
        self.app.call_from_thread(self._show_results, rows)

    def _show_results(self, rows: list[tuple[str, str, str]]) -> None:
        table = self.query_one("#results", DataTable)
        table.loading = False
        table.clear()
        installed = self.ehm.session.package_names()
        for attr, version, desc in rows[:200]:
            name = Text(attr, style="green") if attr in installed else Text(attr)
            table.add_row(name, version, desc, key=attr)
        if not rows:
            self.app.notify("No results.")

    @on(DataTable.RowSelected, "#results")
    def _install(self, event: DataTable.RowSelected) -> None:
        name = str(event.row_key.value)
        if name in self.ehm.session.package_names():
            self.app.notify(f"{name} is already installed.")
            return

        def add(session: Session) -> None:
            session.save(session.file_with_packages().add_list_item(PACKAGES, name))

        self.ehm.stage(add, f"Staged install of {name}")

    def action_remove(self) -> None:
        table = self.query_one("#installed", DataTable)
        if not table.row_count:
            return
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if not key:
            self.app.notify("Complex entries must be edited by hand (`ehm edit`).", severity="warning")
            return
        kind, _, name = key.partition(":")
        if kind == "pkg":
            self.ehm.stage(lambda s: s.remove_package(name), f"Staged removal of {name}")
        else:
            self.ehm.stage(
                lambda s: options.unset_option(s, ("programs", name, "enable")),
                f"Staged disabling programs.{name}",
            )


class ProgramsPane(Pane):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.modules: list[str] = []
        self.loaded = False
        self.path: tuple[str, ...] = ()
        self.options: list[options.Option] = []

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(classes="sidebar"):
                yield Input(placeholder="Filter, or path + Enter", id="filter")
                yield OptionList(id="program-list")
            with Vertical(classes="column"):
                yield Label("Select a program on the left.", id="title")
                yield DataTable(id="options", cursor_type="row")
                yield Static("", id="help", classes="help")

    def on_mount(self) -> None:
        table = self.query_one("#options", DataTable)
        table.add_column("Option", key="name")
        table.add_column("Current", key="current")
        table.add_column("Type", key="type")

    def activated(self) -> None:
        if not self.loaded:
            self.loaded = True
            self.query_one("#program-list").loading = True
            self._load_modules()

    @work(thread=True, exclusive=True, group="modules")
    def _load_modules(self) -> None:
        try:
            modules = self.ehm.ctx.program_modules()
        except ui.EhmError as e:
            self.app.call_from_thread(self.query_one("#title", Label).update, Text(str(e), style="yellow"))
            modules = []
        self.app.call_from_thread(self._set_modules, modules)

    def _set_modules(self, modules: list[str]) -> None:
        self.modules = modules
        self.query_one("#program-list").loading = False
        self._filter(self.query_one("#filter", Input).value)

    def refresh_data(self) -> None:
        if self.modules:
            self._filter(self.query_one("#filter", Input).value)

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self._filter(event.value)

    def _filter(self, query: str) -> None:
        enabled = self.ehm.session.enabled_programs()
        listing = self.query_one("#program-list", OptionList)
        listing.clear_options()
        query = query.lower()
        listing.add_options(
            ListOption(Text.assemble(("● " if m in enabled else "  ", "green"), m), id=m)
            for m in self.modules if query in m.lower()
        )

    @on(Input.Submitted, "#filter")
    def _open_path(self, event: Input.Submitted) -> None:
        if "." in event.value:
            try:
                self._open(options.normalize_program(event.value.strip()))
            except ModuleShapeError as e:
                self.ehm.report_error(e)

    @on(OptionList.OptionSelected, "#program-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self._open(("programs", event.option.id))

    def _open(self, path: tuple[str, ...]) -> None:
        self.path = path
        self.query_one("#title", Label).update(Text(f"Options for {format_path(path)}", style="bold"))
        self.query_one("#options").loading = True
        self._load_options(path)

    @work(thread=True, exclusive=True, group="options")
    def _load_options(self, path: tuple[str, ...]) -> None:
        try:
            found = options.fetch_options(self.ehm.ctx, path)
        except ui.EhmError as e:
            self.app.call_from_thread(self.ehm.report_error, e)
            found = []
        self.app.call_from_thread(self._show_options, path, found)

    def _show_options(self, path: tuple[str, ...], found: list[options.Option]) -> None:
        if path != self.path:
            return
        found.sort(key=lambda o: (o.path[-1] != "enable", o.is_freeform, o.kind == "package", o.path))
        self.options = found
        table = self.query_one("#options", DataTable)
        table.loading = False
        table.clear()
        for i, o in enumerate(found):
            table.add_row(format_path(o.path[len(path):]), o.current_text[:40], o.type[:40], key=str(i))
        if not found:
            self.query_one("#help", Static).update(Text(f"No options under {format_path(path)}.", style="yellow"))
        else:
            table.focus()

    @on(DataTable.RowHighlighted, "#options")
    def _describe(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is None:
            return
        o = self.options[int(event.row_key.value)]
        text = Text(format_path(o.path) + "\n", style="bold")
        text.append(o.description.strip()[:800] or "(no description)")
        if o.example:
            text.append(f"\nExample: {' '.join(o.example.split())[:200]}", style="dim")
        self.query_one("#help", Static).update(text)

    @on(DataTable.RowSelected, "#options")
    def _edit(self, event: DataTable.RowSelected) -> None:
        row_key = event.row_key
        option = self.options[int(row_key.value)]

        def done(result: tuple[tuple[str, ...], str] | None) -> None:
            if not result:
                return
            path, expr = result
            if self.ehm.stage(lambda s: options.set_option(s, path, expr), f"Staged {format_path(path)} = {expr}"):
                if path == option.path:
                    self.query_one("#options", DataTable).update_cell(
                        row_key, "current", Text(f"{expr}  (staged)", style="yellow")
                    )

        self.app.push_screen(EditOptionScreen(option), done)


class DotfilesPane(Pane):
    BINDINGS = [Binding("x,delete", "release", "Stop managing")]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entries: list[dotfiles.Entry] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Input(placeholder="Path to adopt, e.g. ~/.config/ghostty", id="path")
            yield Checkbox("Copy (read-only)", id="copy")
            yield Button("Add", id="add", variant="primary")
        yield DataTable(id="dotfile-table", cursor_type="row")
        yield Static(
            "Symlinked files take edits immediately; copies update on apply. Use the CLI with "
            "--allow-secrets to override secret detection.",
            classes="muted",
        )

    def on_mount(self) -> None:
        table = self.query_one("#dotfile-table", DataTable)
        table.add_column("Target", key="target")
        table.add_column("Status", key="status")
        table.add_column("Mode", key="mode")
        table.add_column("Source", key="source")

    def refresh_data(self) -> None:
        session = self.ehm.session
        pending = load_pending(self.ehm.ctx)
        self.entries = dotfiles.entries(session)
        table = self.query_one("#dotfile-table", DataTable)
        table.clear()
        for i, e in enumerate(self.entries):
            status = dotfiles.entry_status(e, pending)
            mode = "symlink" if e.linked else ("inline" if e.inline else "copy")
            source = ui.tilde(e.source) if e.source else (e.source_expr or "")
            table.add_row(ui.tilde(e.target), Text(status.value, style=_STATUS_STYLE[status]), mode, source, key=str(i))
        for p in pending:
            if p["action"] == "release":
                table.add_row(ui.tilde(p["target"]), Text("release pending", style="yellow"), "", "")

    @on(Button.Pressed, "#add")
    @on(Input.Submitted, "#path")
    def _add(self) -> None:
        field = self.query_one("#path", Input)
        raw = field.value.strip()
        if not raw:
            return
        copy = self.query_one("#copy", Checkbox).value
        if self.ehm.stage(lambda s: dotfiles.plan_add(self.ehm.ctx, s, raw, copy=copy), f"Staged adopting {raw}"):
            field.value = ""
            self.query_one("#dotfile-table", DataTable).focus()

    def action_release(self) -> None:
        table = self.query_one("#dotfile-table", DataTable)
        if not table.row_count:
            return
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if key is None:
            return
        entry = self.entries[int(key)]

        def done(choice: str | None) -> None:
            if choice:
                self.ehm.stage(
                    lambda s: dotfiles.plan_remove(self.ehm.ctx, s, str(entry.target), delete=choice == "delete"),
                    f"Staged releasing {ui.tilde(entry.target)}",
                )

        self.app.push_screen(ReleaseScreen(entry), done)


_STATUS_STYLE = {
    dotfiles.Status.LINKED: "green",
    dotfiles.Status.PENDING: "yellow",
    dotfiles.Status.CONFLICT: "red",
    dotfiles.Status.NOT_APPLIED: "red",
}


def _level_style(level: suggest.Level) -> str:
    return {
        suggest.Level.IMPORTANT: "bold red",
        suggest.Level.RECOMMENDED: "cyan",
        suggest.Level.OPTIONAL: "magenta",
        suggest.Level.INFO: "dim",
    }[level]


# ---- application ----------------------------------------------------------
class EhmApp(App[None]):
    TITLE = "ehm"
    CSS = """
    Pane { padding: 1 1 0 1; }
    .toolbar { height: auto; margin-bottom: 1; }
    .toolbar Input { width: 1fr; }
    .toolbar Button, .toolbar Checkbox { margin-left: 1; }
    .column { width: 1fr; padding-right: 1; }
    .sidebar { width: 36; padding-right: 1; }
    .help { height: auto; max-height: 12; border-top: solid $panel; padding-top: 1; }
    .muted { color: $text-muted; margin-top: 1; }
    .error { color: $error; }
    #suggestion-list { height: 1fr; }
    #program-list { height: 1fr; }
    ModalScreen { align: center middle; }
    .dialog { width: 72; height: auto; max-height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    .dialog.wide { width: 96; }
    .dialog.full { width: 95%; height: 90%; }
    .dialog > * { margin-bottom: 1; }
    .dialog #diff { height: 1fr; }
    .buttons { height: auto; align-horizontal: right; margin-bottom: 0; }
    .buttons Button { margin-left: 1; }
    """
    BINDINGS = [
        Binding("s", "review", "Review & save"),
        Binding("a", "apply", "Apply"),
        Binding("c", "check", "Preview build"),
        Binding("u", "update", "Update"),
        Binding("e", "edit", "Edit file", show=False),
        Binding("g", "generations", "Generations", show=False),
        Binding("b", "rollback", "Rollback", show=False),
        Binding("r", "reload", "Reload", show=False),
        Binding("q", "quit", "Quit"),
        *(Binding(str(i + 1), f"show_tab('{tab}')", show=False) for i, tab in enumerate(
            ("overview", "suggestions", "packages", "programs", "dotfiles")
        )),
    ]

    def __init__(self, ctx: Context):
        super().__init__()
        self.ctx = ctx
        self.session = Session(ctx)
        self._main_thread = threading.get_ident()

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="tab-overview"):
            with TabPane("Overview", id="tab-overview"):
                yield OverviewPane()
            with TabPane("Suggestions", id="tab-suggestions"):
                yield SuggestionsPane()
            with TabPane("Packages", id="tab-packages"):
                yield PackagesPane()
            with TabPane("Programs", id="tab-programs"):
                yield ProgramsPane()
            with TabPane("Dotfiles", id="tab-dotfiles"):
                yield DotfilesPane()
        yield Footer()

    def on_mount(self) -> None:
        self._main_thread = threading.get_ident()
        self.call_after_refresh(self.refresh_views)

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = f"tab-{tab}"

    @on(TabbedContent.TabActivated)
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        for pane in event.pane.query(Pane):
            pane.activated()

    # -- messages from core modules ----------------------------------------
    def ui_sink(self, kind: str, message: str) -> None:
        """Receives ``ui.warn``/``ui.error``/... output while the TUI owns the terminal."""
        if threading.get_ident() != self._main_thread:
            self.call_from_thread(self.ui_sink, kind, message)
            return
        if kind == "error":
            self.notify(message, severity="error", markup=False)
        elif kind == "warn":
            self.notify(message, severity="warning", markup=False)
        elif kind == "ok":
            self.notify(message, markup=False)
        else:
            self.log(message)

    def report_error(self, error: Exception) -> None:
        message = str(error)
        if isinstance(error, ModuleShapeError):
            message += "\nPlease edit it manually (press e)."
        self.notify(message, severity="error", timeout=10, markup=False)

    # -- staging ------------------------------------------------------------
    def stage(self, change: Callable[[Session], object], description: str) -> bool:
        """Apply ``change`` to the staging session. Returns ``False`` on error."""
        try:
            change(self.session)
        except EDITABLE_ERRORS as e:
            self.report_error(e)
            return False
        self.notify(f"{description}. Press s to review and save.", markup=False)
        self.refresh_views()
        return True

    def refresh_views(self) -> None:
        count = len(self.session.changed_files()) + len(self.session.notes)
        status = f"{count} unsaved change(s)" if self.session.has_changes() else "no unsaved changes"
        self.sub_title = f"{__version__} · {MODE_LABELS[self.ctx.mode]} · {status}"
        for pane in self.query(Pane):
            try:
                pane.refresh_data()
            except EDITABLE_ERRORS as e:
                self.report_error(e)

    def reset_session(self) -> None:
        self.session = Session(self.ctx)
        self.refresh_views()

    def action_reload(self) -> None:
        if self.session.has_changes():
            self.notify("Save or discard the staged changes first (s).", severity="warning")
            return
        self.reset_session()

    # -- saving -------------------------------------------------------------
    def action_review(self) -> None:
        if not self.session.has_changes():
            self.notify("Nothing to save.")
            return
        self.push_screen(ReviewScreen(self.session), self._reviewed)

    def _reviewed(self, choice: str | None) -> None:
        if choice == "discard":
            self.reset_session()
            self.notify("Discarded all staged changes.")
        elif choice == "save":
            self._save()

    def _save(self) -> None:
        session = self.session
        try:
            session.validate()
            snapshot = session.write()
        except EDITABLE_ERRORS as e:
            self.report_error(e)
            return
        if not self.ctx.is_flake:
            self._saved()
            return
        self.notify("Saved. Checking the configuration...")
        self._verify(session, snapshot)

    @work(thread=True, exclusive=True, group="verify")
    def _verify(self, session: Session, snapshot: dict[Path, str]) -> None:
        problem = evaluate(self.ctx)
        self.call_from_thread(self._verified, session, snapshot, problem)

    def _verified(self, session: Session, snapshot: dict[Path, str], problem: str | None) -> None:
        if problem is None:
            self._saved()
            return

        def answer(revert: bool | None) -> None:
            if revert:
                session.revert(snapshot)
                self.notify("Reverted.")
            self.reset_session()

        self.push_screen(
            ConfirmScreen(f"The configuration failed to evaluate:\n\n{problem}\n\nRevert the saved files?",
                          yes="Revert", no="Keep", danger=True),
            answer,
        )

    def _saved(self) -> None:
        self.reset_session()
        self.notify("Saved. Press a to apply.")

    # -- terminal operations ------------------------------------------------
    async def in_terminal(self, operation: Callable[[], None], *, needs_saved: bool = True) -> None:
        """Suspend the UI and run ``operation`` with the real terminal (sudo, build logs, prompts)."""
        if needs_saved and self.session.has_changes():
            self.notify("Save or discard the staged changes first (s).", severity="warning")
            return
        if any(w.is_running for w in self.workers):
            # Background evaluations would compete with the build for Nix's evaluation
            # cache ("SQLite database ... is busy") and print over the handed-over terminal.
            self.notify("Waiting for background evaluation to finish...")
            await self.workers.wait_for_complete()
        try:
            with self.suspend(), ui.redirect(None):
                try:
                    operation()
                except EDITABLE_ERRORS as e:
                    ui.error(str(e))
                except KeyboardInterrupt:
                    print()
                try:
                    input("\nPress Enter to return to ehm...")
                except (EOFError, KeyboardInterrupt):
                    pass
        except Exception as e:  # e.g. SuspendNotSupported in environments without a real terminal
            self.notify(f"Cannot hand over the terminal here: {e}", severity="error", markup=False)
            return
        self.reset_session()

    async def action_apply(self) -> None:
        await self.in_terminal(lambda: applier.apply(self.ctx))

    async def action_check(self) -> None:
        await self.in_terminal(lambda: applier.check(self.ctx))

    async def action_update(self) -> None:
        await self.in_terminal(lambda: applier.update(self.ctx, []))

    async def action_generations(self) -> None:
        await self.in_terminal(lambda: applier.generations(self.ctx), needs_saved=False)

    async def action_rollback(self) -> None:
        await self.in_terminal(lambda: applier.rollback(self.ctx), needs_saved=False)

    async def action_edit(self) -> None:
        await self.in_terminal(lambda: dotfiles.open_editor(self.ctx.home_file))

    async def action_quit(self) -> None:
        if not self.session.has_changes():
            self.exit()
            return
        self.push_screen(
            ConfirmScreen("Quit and discard the staged changes?", yes="Quit", danger=True),
            lambda yes: self.exit() if yes else None,
        )


def run(ctx: Context) -> None:
    app = EhmApp(ctx)
    with ui.redirect(app.ui_sink):
        app.run()
