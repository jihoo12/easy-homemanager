"""Command-line entry point and interactive menu."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, dotfiles, options, suggest, ui
from . import apply as applier
from .context import (
    MODE_LABELS,
    NIX,
    Context,
    detect,
    last_error,
    settings_path,
    state_dir,
    xdg_dir,
)
from .nixfile import ModuleShapeError, format_path, split_option_path, to_nix
from .workspace import (
    PACKAGES,
    Session,
    git,
    is_git_repo,
    load_pending,
    untracked_files,
    write_atomic,
)

ATTR_RE = re.compile(r"^[A-Za-z_][\w'\-]*(\.[A-Za-z_][\w'\-]*)*$")
MANUAL_EDIT_HINT = "Please edit it manually with `ehm edit`."


# ---- shared save/verify/apply flow ------------------------------------
def verify_after_save(ctx: Context, session: Session, snapshot: dict[Path, str]) -> bool:
    """Evaluate the configuration right after saving and offer to revert on failure."""
    if not ctx.is_flake:
        return True
    ui.info("Checking the updated configuration...")
    r = subprocess.run(
        [*NIX, "eval", "--raw", f"{ctx.activation_attr()}.drvPath"], capture_output=True, text=True
    )
    if r.returncode == 0:
        ui.ok("Configuration check passed")
        return True
    ui.error("Configuration failed to evaluate:\n" + last_error(r.stderr))
    if ui.confirm("Revert the files that were just changed?", default=True):
        for path, text in snapshot.items():
            write_atomic(path, text)
        for undo in session.undo:
            undo()
        ui.ok("Reverted.")
    return False


def offer_apply(ctx: Context) -> None:
    if ui.ASSUME_YES:
        ui.info("Run `ehm apply` to activate the changes.")
    elif ui.confirm("Apply now?"):
        applier.apply(ctx)
    else:
        ui.info("You can apply later with `ehm apply`.")


def finish(ctx: Context, session: Session, args=None, question: str = "Save these changes?") -> bool:
    """Diff → confirm → save → verify → (optionally) apply."""
    snapshot = {p: session.originals[p] for p in session.changed_files()}
    if not session.commit(question):
        return False
    ui.ok("Saved.")
    if not verify_after_save(ctx, session, snapshot):
        raise ui.EhmError("The configuration did not pass the check.")
    if args is not None and getattr(args, "apply", False):
        applier.apply(ctx, backup_conflicts=getattr(args, "backup_conflicts", False))
    else:
        offer_apply(ctx)
    return True


# ---- packages ---------------------------------------------------------
def cmd_add(ctx: Context, args) -> None:
    session = Session(ctx)
    names = session.package_names()
    for raw in args.packages:
        name = raw.removeprefix("nixpkgs#").removeprefix("pkgs.")
        if not ATTR_RE.match(name):
            raise ui.EhmError(f"Invalid package name: {raw}")
        if name in names:
            ui.info(f"{name} is already installed.")
            continue
        if not args.no_check:
            found = ctx.package_info(name)
            if found is None:
                ui.warn(f"'{name}' does not exist in nixpkgs.")
                if not ui.ASSUME_YES and ui.confirm("Search for similar packages?", default=True):
                    run_search(ctx, [name], limit=10)
                raise ui.EhmError(f"'{name}' was not added.")
            desc = f" — {found['description']}" if found.get("description") else ""
            ui.ok(f"{found.get('name') or name}{desc}")
        nf = session.file_with_packages()
        session.save(nf.add_list_item(PACKAGES, name))
        names.add(name)
        tip_program(ctx, name)
    if session.has_changes():
        finish(ctx, session, args)


def tip_program(ctx: Context, name: str) -> None:
    if not ctx.is_flake:
        return
    try:
        if name in ctx.program_modules():
            ui.info(ui.dim(f"Tip: to manage its configuration as well, use `ehm enable {name}`."))
    except ui.EhmError:
        pass


def cmd_remove(ctx: Context, args) -> None:
    session = Session(ctx)
    for raw in args.packages:
        name = raw.removeprefix("pkgs.")
        if session.remove_package(name):
            continue
        hint = ""
        if any(nf.has_path(("programs", name, "enable")) for nf in session.modules()):
            hint = f"\nIt is enabled through programs.{name}; use `ehm disable {name}`."
        else:
            complex_items = [n for _, n, simple in session.packages() if not simple and name in n]
            if complex_items:
                hint = f"\nIt is part of a complex entry: {complex_items[0]}\n{MANUAL_EDIT_HINT}"
        raise ui.EhmError(f"'{name}' was not found in home.packages.{hint}")
    finish(ctx, session, args)


def cmd_list(ctx: Context, args) -> None:
    if args.all:
        ctx.require_flake()
        names = set(ctx.nix_eval_json(
            f"{ctx.hm_config_attr()}.home.packages",
            'map (p: p.name or (p.pname or "?"))',
        ))
        for name in sorted(names):
            print(name)
        ui.info(ui.dim(f"{len(names)} total (including packages installed by programs.* modules)"))
        return
    session = Session(ctx)
    items = session.packages()
    for _, name, simple in items:
        print(name if simple else ui.dim(name))
    enabled = session.enabled_programs()
    if enabled:
        print(ui.bold("\nEnabled through programs.*"))
        for name in sorted(enabled):
            print(name)
    print()
    ui.info(ui.dim(f"{len(items)} package(s), {len(enabled)} module(s)"))


def run_search(ctx: Context, terms: list[str], limit: int = 25) -> None:
    ui.info("Searching... (the first search can take about a minute)")
    cmd = [*NIX, "search", *ctx.inputs_args(), "nixpkgs", *terms, "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ui.EhmError("Search failed:\n" + last_error(r.stderr))
    data = json.loads(r.stdout or "{}")
    installed = Session(ctx).package_names()
    rows = []
    for key, value in data.items():
        attr = key.split(".", 2)[2] if key.count(".") >= 2 else key
        rows.append((attr, value.get("version", ""), value.get("description", "")))
    needle = terms[0].lower()
    rows.sort(key=lambda r: (r[0].lower() != needle, not r[0].lower().startswith(needle), len(r[0]), r[0]))
    if not rows:
        ui.info("No results.")
        return
    for attr, version, desc in rows[:limit]:
        mark = ui.paint(ui.GREEN, " [installed]") if attr in installed else ""
        print(f"{ui.bold(attr)} {ui.dim(version)}{mark}")
        if desc:
            print(f"    {desc}")
    if len(rows) > limit:
        ui.info(ui.dim(f"... and {len(rows) - limit} more. Try a more specific query."))
    print(ui.dim("\nInstall with: ehm add <name>"))


def cmd_search(ctx: Context, args) -> None:
    run_search(ctx, args.terms, args.limit)


# ---- dotfiles ---------------------------------------------------------
def cmd_dot_add(ctx: Context, args) -> None:
    session = Session(ctx)
    for path in args.paths:
        dotfiles.plan_add(ctx, session, path, copy=args.copy, allow_secrets=args.allow_secrets)
    finish(ctx, session, args)


def cmd_dot_rm(ctx: Context, args) -> None:
    session = Session(ctx)
    for query in args.targets:
        dotfiles.plan_remove(ctx, session, query, delete=args.delete)
    finish(ctx, session, args)


def cmd_dot_list(ctx: Context, args) -> None:
    session = Session(ctx)
    pending = load_pending(ctx)
    items = dotfiles.entries(session)
    if not items:
        ui.info("No managed dotfiles. Add one with `ehm dot add <file>`.")
    for e in items:
        how = "symlink" if e.linked else ("inline" if e.inline else "copy")
        status = dotfiles.entry_status(e, pending)
        src = ui.tilde(e.source) if e.source else (e.source_expr or "")
        print(f"{ui.bold(ui.tilde(e.target))}  {ui.paint(status.color, status.value)}")
        print(ui.dim(f"    {how}: {src}"))
    for p in pending:
        if p["action"] == "release":
            print(f"{ui.tilde(p['target'])}  {ui.paint(ui.YELLOW, 'release pending')}")
    if args.all:
        gen = applier.current_generation()
        if gen:
            print(ui.bold("\nAll files created by the current generation (including programs.*)"))
            for rel in sorted(applier.home_file_leaves(gen)):
                print(f"~/{rel}")


def cmd_dot_edit(ctx: Context, args) -> None:
    session = Session(ctx)
    entry = dotfiles.find_entry(session, args.target)
    if entry.source is None:
        ui.info(f"{ui.tilde(entry.target)} is defined inline in Nix; opening the module file.")
        dotfiles.open_editor(entry.nix_file.path)
        return
    if entry.source.is_dir():
        raise ui.EhmError(f"This is a directory; open the file you need inside it: {entry.source}")
    dotfiles.open_editor(entry.source)
    if entry.linked:
        ui.ok("The file is symlinked, so changes take effect immediately.")
    else:
        ui.info("The file is copied at build time; run `ehm apply` to activate changes.")


# ---- options ----------------------------------------------------------
def cmd_programs(ctx: Context, args) -> None:
    ctx.require_flake()
    modules = ctx.program_modules()
    enabled = Session(ctx).enabled_programs()
    query = (args.query or "").lower()
    shown = [m for m in modules if query in m.lower()]
    for name in shown:
        mark = ui.paint(ui.GREEN, "  [enabled]") if name in enabled else ""
        print(f"{name}{mark}")
    ui.info(ui.dim(f"{len(shown)} found. Enable: ehm enable <name>. Configure: ehm configure <name>."))


def _check_program(ctx: Context, path: tuple[str, ...]) -> None:
    if ctx.is_flake and len(path) == 2 and path[0] == "programs":
        modules = ctx.program_modules()
        if path[1] not in modules:
            close = [m for m in modules if path[1].lower() in m.lower()][:8]
            hint = f"\nSimilar names: {', '.join(close)}" if close else ""
            raise ui.EhmError(
                f"There is no programs.{path[1]} module.{hint}\n"
                f"To install just the package, use `ehm add {path[1]}`."
            )


def cmd_enable(ctx: Context, args) -> None:
    session = Session(ctx)
    for name in args.names:
        path = options.normalize_program(name)
        _check_program(ctx, path)
        options.set_option(session, (*path, "enable"), "true")
        if len(path) == 2 and path[0] == "programs" and session.remove_package(path[1]):
            session.notes.append(f"Removed {path[1]} from home.packages; the module installs it.")
    finish(ctx, session, args)


def cmd_disable(ctx: Context, args) -> None:
    session = Session(ctx)
    for name in args.names:
        path = options.normalize_program(name)
        if not options.unset_option(session, (*path, "enable")):
            raise ui.EhmError(f"{format_path(path)}.enable is not set.")
    finish(ctx, session, args)


def cmd_set(ctx: Context, args) -> None:
    path = split_option_path(args.option)
    session = Session(ctx)
    if args.nix:
        expr = args.value
    elif args.str:
        expr = to_nix(args.value)
    else:
        expr = to_nix(options.parse_auto(args.value))
    options.set_option(session, path, expr)
    finish(ctx, session, args)


def cmd_unset(ctx: Context, args) -> None:
    path = split_option_path(args.option)
    session = Session(ctx)
    if not options.unset_option(session, path):
        raise ui.EhmError(f"{format_path(path)} is not set.")
    finish(ctx, session, args)


def cmd_get(ctx: Context, args) -> None:
    path = split_option_path(args.option)
    session = Session(ctx)
    for nf in session.modules():
        b = nf.find(path)
        if b:
            print(ui.dim(f"# {ui.tilde(nf.path)}"))
            print(nf.value_text(b))
    if ctx.is_flake:
        attr = f"{ctx.hm_config_attr()}.{format_path(path)}"
        r = subprocess.run([*NIX, "eval", "--json", attr], capture_output=True, text=True)
        if r.returncode != 0:
            raise ui.EhmError("Could not read the value:\n" + last_error(r.stderr, 4))
        print(ui.dim("# effective value"))
        print(r.stdout.strip())


def cmd_configure(ctx: Context, args) -> None:
    name = args.program
    if not name:
        ctx.require_flake()
        enabled = sorted(Session(ctx).enabled_programs())
        if enabled:
            ui.info("Enabled programs: " + ", ".join(enabled))
        name = ui.ask("Program to configure (e.g. git)")
        if not name:
            return
    session = options.configure(ctx, name)
    if session and session.has_changes():
        finish(ctx, session, args)


# ---- suggestions and bundles ------------------------------------------
def _print_suggestion(prefix: str, s: suggest.Suggestion) -> None:
    print(f"{prefix} {ui.paint(s.level.color, f'[{s.level.label}]')} {s.title}")
    print(ui.dim(f"      {s.why}"))


def cmd_suggest(ctx: Context, args) -> None:
    session = Session(ctx)
    ui.step("Analyzing the current configuration")
    items = suggest.analyze(ctx, session)
    if not items:
        ui.ok("No suggestions. Your configuration looks tidy.")
        return
    fixable = [s for s in items if s.fix]
    for i, s in enumerate(fixable, 1):
        _print_suggestion(f"  {i:>2})", s)
    for s in items:
        if not s.fix:
            _print_suggestion("   ·", s)
    if not fixable:
        return
    if args.report:
        ui.info(ui.dim("Run `ehm suggest` without --report to pick suggestions to apply."))
        return
    print()
    if ui.ASSUME_YES and args.all:
        chosen = list(range(len(fixable)))
    else:
        chosen = ui.choose_many("Select suggestions to apply", len(fixable))
    if not chosen:
        ui.info("Nothing changed.")
        return
    session = Session(ctx)
    for idx in chosen:
        try:
            fixable[idx].fix(session)
        except ui.EhmError as e:
            ui.warn(f"Skipped '{fixable[idx].title}': {e}")
    finish(ctx, session, args)


def cmd_setup(ctx: Context, args) -> None:
    ctx.require_flake()
    modules = set(ctx.program_modules())
    enabled = Session(ctx).enabled_programs()
    flat: list[str] = []
    for group, programs in suggest.BUNDLES.items():
        print(ui.bold(group))
        for name, desc in programs.items():
            if name not in modules:
                continue
            if name in enabled:
                print(ui.dim(f"       {name}: already enabled"))
                continue
            flat.append(name)
            print(f"  {len(flat):>3}) {name}: {desc}")
    if not flat:
        ui.ok("All recommended programs are already enabled.")
        return
    picked = ui.choose_many("Select programs to enable", len(flat))
    if not picked:
        return
    names = [flat[i] for i in picked]
    if "zsh" in names and (Path.home() / ".zshrc").exists():
        ui.warn("programs.zsh replaces ~/.zshrc. The existing file will be moved to the backup directory on apply.")
    cmd_enable(ctx, argparse.Namespace(names=names, apply=getattr(args, "apply", False)))


# ---- system -----------------------------------------------------------
def cmd_apply(ctx: Context, args) -> None:
    applier.apply(ctx, backup_conflicts=args.backup_conflicts, commit=args.commit, push=args.push)


def cmd_check(ctx: Context, args) -> None:
    applier.check(ctx)


def cmd_update(ctx: Context, args) -> None:
    applier.update(ctx, args.inputs, do_apply=not args.no_apply, backup_conflicts=args.backup_conflicts)


def cmd_generations(ctx: Context, args) -> None:
    applier.generations(ctx)


def cmd_rollback(ctx: Context, args) -> None:
    applier.rollback(ctx)


def cmd_gc(ctx: Context, args) -> None:
    applier.collect_garbage(ctx, args.older_than)


def cmd_edit(ctx: Context, args) -> None:
    before = ctx.home_file.read_text()
    dotfiles.open_editor(ctx.home_file)
    if ctx.home_file.read_text() != before and verify_after_save(ctx, Session(ctx), {}):
        offer_apply(ctx)


def cmd_status(ctx: Context, args) -> None:
    session = Session(ctx)
    rows = [
        ("Mode", MODE_LABELS[ctx.mode]),
        ("Directory", ui.tilde(ctx.config_dir)),
        ("Home file", ui.tilde(ctx.home_file)),
        ("User", ctx.user + (f"   target: {ctx.host}" if ctx.host else "")),
    ]
    files = session.module_files()
    if len(files) > 1:
        rows.append(("Modules", ", ".join(ui.tilde(f) for f in files[1:])))
    _print_section("Configuration", rows)
    _print_section("Managed", [
        ("Packages", str(len(session.packages()))),
        ("Programs", str(len(session.enabled_programs()))),
        ("Dotfiles", str(len(dotfiles.entries(session)))),
    ])
    pending = load_pending(ctx)
    if pending:
        _print_section("Pending", [
            ("adopt" if p["action"] == "adopt" else "release", ui.tilde(p["target"])) for p in pending
        ])
    if is_git_repo(ctx):
        changed = [line for line in git(ctx, "status", "--short").stdout.splitlines() if line]
        untracked = untracked_files(ctx)
        summary = f"{len(changed)} uncommitted change(s)" + (f", {len(untracked)} untracked" if untracked else "")
        _print_section("Git", [("Status", summary)])
    else:
        print(ui.dim("Not a git repository. `git init` is recommended to keep a history of changes."))
    for nf in session.modules():
        if nf.shape_error:
            ui.warn(f"{ui.tilde(nf.path)}: cannot be edited automatically — {nf.shape_error}")


def _print_section(title: str, rows: list[tuple[str, str]]) -> None:
    print(ui.bold(title))
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key:<{width}}  {value}")


def cmd_config() -> None:
    path = settings_path()
    print(f"Settings file: {path}" + ("" if path.exists() else " (not created yet; using defaults)"))
    print("Available keys:")
    print('  config_dir = "/etc/nixos"        # configuration directory')
    print('  home_file = "home.nix"           # relative to config_dir')
    print('  host = "nixos"                   # configuration name inside the flake')
    print('  dotfiles_dir = "dotfiles"        # where adopted dotfiles are stored')
    print('  extra_rebuild_args = ["--show-trace"]')
    print("  auto_commit = false              # commit after a successful apply")
    print("  auto_push = false                # push after auto-commit")
    print(f"Backups and state: {state_dir()}")


FLAKE_TEMPLATE = """{
  description = "Home Manager configuration for @USER@";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-manager, ... }: {
    homeConfigurations."@USER@" = home-manager.lib.homeManagerConfiguration {
      pkgs = nixpkgs.legacyPackages.@SYSTEM@;
      modules = [ ./home.nix ];
    };
  };
}
"""

HOME_TEMPLATE = """{ config, pkgs, ... }:

{
  home.username = "@USER@";
  home.homeDirectory = "@HOME@";
  home.stateVersion = "@STATE@"; # The release you first installed. Do not change it.

  home.packages = with pkgs; [
  ];

  programs.home-manager.enable = true;
}
"""


def cmd_init(args) -> None:
    directory = (
        Path(args.dir).expanduser().resolve() if args.dir
        else xdg_dir("XDG_CONFIG_HOME", ".config") / "home-manager"
    )
    if (directory / "flake.nix").exists() or (directory / "home.nix").exists():
        raise ui.EhmError(f"A configuration already exists in {directory}")
    if shutil.which("nix") is None:
        raise ui.EhmError("nix was not found. Please install Nix first.")
    system = subprocess.run(
        [*NIX, "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"],
        capture_output=True, text=True,
    ).stdout.strip()
    state = subprocess.run(
        [*NIX, "eval", "--raw", "nixpkgs#lib.trivial.release"], capture_output=True, text=True
    ).stdout.strip()
    if not system or not state:
        raise ui.EhmError("Could not determine the system or nixpkgs release. Check your network connection.")
    user = getpass.getuser()
    values = {"@USER@": user, "@HOME@": str(Path.home()), "@SYSTEM@": system, "@STATE@": state}

    def fill(template: str) -> str:
        for key, value in values.items():
            template = template.replace(key, value)
        return template

    ui.info(f"Creating flake.nix and home.nix in {directory} (user {user}, {system}).")
    if not ui.confirm("Continue?", default=True):
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "flake.nix").write_text(fill(FLAKE_TEMPLATE))
    (directory / "home.nix").write_text(fill(HOME_TEMPLATE))
    if not (directory / ".git").exists():
        subprocess.run(["git", "-C", str(directory), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(directory), "add", "flake.nix", "home.nix"], check=True)
    ui.ok("Done. Next, run `ehm apply`.")


# ---- interactive menu -------------------------------------------------
def _ns(**kw) -> argparse.Namespace:
    kw.setdefault("apply", False)
    kw.setdefault("backup_conflicts", False)
    return argparse.Namespace(**kw)


def menu(ctx: Context) -> None:
    def install() -> None:
        name = ui.ask("Package(s) to install (space-separated)")
        if name:
            cmd_add(ctx, _ns(packages=name.split(), no_check=False))

    def search() -> None:
        q = ui.ask("Search term")
        if q:
            run_search(ctx, q.split())

    def remove() -> None:
        items = sorted(Session(ctx).package_names())
        if not items:
            ui.info("No packages installed.")
            return
        for i, n in enumerate(items, 1):
            print(f"  {i:>3}) {n}")
        picked = ui.choose_many("Select packages to remove", len(items))
        if picked:
            cmd_remove(ctx, _ns(packages=[items[i] for i in picked]))

    def dot_add() -> None:
        path = ui.ask("File or directory to add (e.g. ~/.config/ghostty)")
        if path:
            cmd_dot_add(ctx, _ns(paths=[path], copy=False, allow_secrets=False))

    def dot_rm() -> None:
        items = dotfiles.entries(Session(ctx))
        if not items:
            ui.info("No managed dotfiles.")
            return
        idx = ui.choose("Stop managing which entry?", [ui.tilde(e.target) for e in items])
        if idx is not None:
            cmd_dot_rm(ctx, _ns(targets=[str(items[idx].target)], delete=False))

    actions = [
        ("Review configuration and suggestions", lambda: cmd_suggest(ctx, _ns(all=False, report=False))),
        ("Pick recommended programs", lambda: cmd_setup(ctx, _ns())),
        ("Configure a program", lambda: cmd_configure(ctx, _ns(program=None))),
        ("Install packages", install),
        ("Search packages", search),
        ("Remove packages", remove),
        ("List packages", lambda: cmd_list(ctx, _ns(all=False))),
        ("Add a dotfile", dot_add),
        ("List dotfiles", lambda: cmd_dot_list(ctx, _ns(all=False))),
        ("Stop managing a dotfile", dot_rm),
        ("Preview changes (no apply)", lambda: cmd_check(ctx, _ns())),
        ("Apply", lambda: cmd_apply(ctx, _ns(commit=False, push=False))),
        ("Update", lambda: cmd_update(ctx, _ns(inputs=[], no_apply=False))),
        ("List generations", lambda: cmd_generations(ctx, _ns())),
        ("Roll back to the previous generation", lambda: cmd_rollback(ctx, _ns())),
        ("Status", lambda: cmd_status(ctx, _ns())),
    ]
    print(ui.bold(f"ehm {__version__}") + ui.dim(f"  —  {MODE_LABELS[ctx.mode]}, {ui.tilde(ctx.home_file)}"))
    while True:
        print()
        idx = ui.choose("What would you like to do?", [label for label, _ in actions])
        if idx is None:
            return
        try:
            actions[idx][1]()
        except ui.EhmError as e:
            ui.error(str(e))
        except ModuleShapeError as e:
            ui.error(f"{e}\n{MANUAL_EDIT_HINT}")


# ---- argument parsing -------------------------------------------------
def _add_command(sub, name: str, func, help: str, aliases=(), edits: bool = False) -> argparse.ArgumentParser:
    """Register a subcommand with the common ``-y`` flag and, for editing commands, ``--apply``."""
    c = sub.add_parser(name, help=help, description=help, aliases=list(aliases))
    c.set_defaults(func=func)
    c.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="skip confirmations")
    if edits:
        c.add_argument("--apply", action="store_true", help="apply right after saving")
        c.add_argument("--backup-conflicts", action="store_true", help="move conflicting files to the backup directory")
    return c


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ehm",
        description="A semi-automatic Home Manager CLI. Run without arguments for an interactive menu.",
    )
    p.add_argument("--version", action="version", version=f"ehm {__version__}")
    p.add_argument("--config", metavar="DIR", help="configuration directory (default: auto-detect)")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmations")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def cmd(*a, **kw):
        return _add_command(sub, *a, **kw)

    cmd("status", cmd_status, "show the detected setup and current state")
    c = cmd("suggest", cmd_suggest, "analyze the configuration and apply selected improvements", edits=True)
    c.add_argument("--report", action="store_true", help="only list suggestions")
    c.add_argument("--all", action="store_true", help="with -y, select every fixable suggestion")
    cmd("setup", cmd_setup, "pick recommended programs to enable", edits=True)

    c = cmd("add", cmd_add, "add packages to home.packages", aliases=["install"], edits=True)
    c.add_argument("packages", nargs="+")
    c.add_argument("--no-check", action="store_true", help="skip checking that the package exists in nixpkgs")
    c = cmd("remove", cmd_remove, "remove packages from home.packages", aliases=["rm", "uninstall"], edits=True)
    c.add_argument("packages", nargs="+")
    c = cmd("list", cmd_list, "list installed packages", aliases=["ls"])
    c.add_argument("--all", action="store_true", help="include packages installed by modules (requires evaluation)")
    c = cmd("search", cmd_search, "search nixpkgs")
    c.add_argument("terms", nargs="+")
    c.add_argument("--limit", type=int, default=25, help="maximum number of results (default: 25)")

    dot = sub.add_parser("dot", help="manage dotfiles", description="manage dotfiles")
    dot_sub = dot.add_subparsers(dest="dot_command", metavar="<command>", required=True)

    def dcmd(*a, **kw):
        return _add_command(dot_sub, *a, **kw)

    c = dcmd("add", cmd_dot_add, "move files into the repository and start managing them", edits=True)
    c.add_argument("paths", nargs="+")
    c.add_argument("--copy", action="store_true", help="copy at build time instead of symlinking (read-only)")
    c.add_argument("--allow-secrets", action="store_true", help="ignore the secret detection")
    c = dcmd("rm", cmd_dot_rm, "stop managing; restore as regular files", aliases=["remove"], edits=True)
    c.add_argument("targets", nargs="+")
    c.add_argument("--delete", action="store_true", help="remove the file instead of restoring it (repository copy is backed up)")
    c = dcmd("list", cmd_dot_list, "list managed dotfiles and their status", aliases=["ls"])
    c.add_argument("--all", action="store_true", help="also show files generated by programs.* modules")
    c = dcmd("edit", cmd_dot_edit, "open a dotfile's source in $EDITOR")
    c.add_argument("target")

    c = cmd("programs", cmd_programs, "find programs that have a Home Manager module")
    c.add_argument("query", nargs="?")
    c = cmd("configure", cmd_configure, "browse and set a program's options", aliases=["conf"], edits=True)
    c.add_argument("program", nargs="?")
    c = cmd("enable", cmd_enable, "enable programs.<name>", edits=True)
    c.add_argument("names", nargs="+")
    c = cmd("disable", cmd_disable, "disable programs.<name>", edits=True)
    c.add_argument("names", nargs="+")
    c = cmd("set", cmd_set, "set an option (e.g. ehm set programs.git.settings.user.name alice)", edits=True)
    c.add_argument("option")
    c.add_argument("value")
    c.add_argument("--nix", action="store_true", help="treat the value as a raw Nix expression")
    c.add_argument("--str", action="store_true", help="always treat the value as a string")
    c = cmd("unset", cmd_unset, "remove an option definition", edits=True)
    c.add_argument("option")
    c = cmd("get", cmd_get, "show an option's definition and effective value")
    c.add_argument("option")

    c = cmd("apply", cmd_apply, "build, preview, resolve conflicts and switch", aliases=["switch"])
    c.add_argument("--backup-conflicts", action="store_true", help="move conflicting files to the backup directory")
    c.add_argument("--commit", action="store_true", help="git commit after applying")
    c.add_argument("--push", action="store_true", help="push after committing (with --commit)")
    cmd("check", cmd_check, "build and preview only (no sudo)", aliases=["preview"])
    c = cmd("update", cmd_update, "update flake inputs and apply")
    c.add_argument("inputs", nargs="*", help="only update these inputs")
    c.add_argument("--no-apply", action="store_true", help="update the lock file without applying")
    c.add_argument("--backup-conflicts", action="store_true", help="move conflicting files to the backup directory")
    cmd("generations", cmd_generations, "list generations", aliases=["gens"])
    cmd("rollback", cmd_rollback, "roll back to the previous generation")
    c = cmd("gc", cmd_gc, "delete old generations and unused store paths")
    c.add_argument("--older-than", default="14d", help="default: 14d")
    cmd("edit", cmd_edit, "open the home configuration file in $EDITOR")
    sub.add_parser("config", help="show ehm's own settings")
    c = sub.add_parser("init", help="create a new standalone Home Manager flake")
    c.add_argument("--dir", help="default: ~/.config/home-manager")
    c.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="skip confirmations")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui.ASSUME_YES = args.yes
    try:
        if args.command == "init":
            cmd_init(args)
        elif args.command == "config":
            cmd_config()
        else:
            ctx = detect(args.config)
            if args.command is None:
                menu(ctx)
            else:
                args.func(ctx, args)
        return 0
    except ui.EhmError as e:
        ui.error(str(e))
        return 1
    except ModuleShapeError as e:
        ui.error(f"{e}\n{MANUAL_EDIT_HINT}")
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
