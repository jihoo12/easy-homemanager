"""Analyze the current configuration and propose improvements.

Analysis never modifies anything; fixes are applied only for the suggestions the
user selects.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import apply as applier
from . import dotfiles, install, ui
from .context import Context
from .nixfile import NixFile, NixParseError, normalize_pkg
from .options import set_option
from .workspace import Session, git_track, untracked_files


class Level(enum.IntEnum):
    """Suggestion severity; lower values sort first."""

    IMPORTANT = 0
    RECOMMENDED = 1
    OPTIONAL = 2
    INFO = 3

    @property
    def label(self) -> str:
        return self.name.lower()

    @property
    def color(self) -> str:
        return {
            Level.IMPORTANT: ui.RED,
            Level.RECOMMENDED: ui.CYAN,
            Level.OPTIONAL: ui.MAGENTA,
            Level.INFO: ui.DIM,
        }[self]


@dataclass
class Suggestion:
    level: Level
    title: str
    why: str
    fix: Callable[[Session], None] | None = None


# Names under ~/.config that hold configuration rather than application state
# (browser profiles and the like are deliberately excluded).
CONFIG_DIRS = {
    "alacritty", "kitty", "foot", "ghostty", "wezterm", "helix", "tmux", "zellij",
    "fcitx5", "waybar", "rofi", "wofi", "mako", "dunst", "fuzzel", "swaylock", "hypr",
    "sway", "i3", "btop", "htop", "yazi", "lazygit", "fastfetch", "bat", "starship.toml",
}
HOME_FILES = [
    ".zshrc", ".zshenv", ".zprofile", ".bashrc", ".bash_profile", ".profile",
    ".gitconfig", ".tmux.conf", ".vimrc", ".inputrc",
]
PROGRAM_NOTES = {
    "vscode": "Extensions and settings can be pinned in Nix.",
    "gh": "The git credential helper can be configured.",
    "ripgrep": "Default arguments can be managed in Nix.",
    "fastfetch": "Displayed modules can be managed in Nix.",
    "ghostty": "Theme and fonts can be managed in Nix.",
}
MAX_SUGGEST_BYTES = 5 * 1024 * 1024


def _migrate(name: str) -> Callable[[Session], None]:
    def fix(session: Session) -> None:
        session.remove_package(name)
        set_option(session, ("programs", name, "enable"), "true")

    return fix


def _remove_package(name: str) -> Callable[[Session], None]:
    def fix(session: Session) -> None:
        session.remove_package(name)

    return fix


def _system_packages(ctx: Context) -> set[str]:
    path = ctx.config_dir / "configuration.nix"
    if not path.exists():
        return set()
    try:
        nf = NixFile(path.read_text(), path)
    except NixParseError:
        return set()
    b = nf.find(("environment", "systemPackages"))
    if b is None:
        return set()
    return {normalize_pkg(i.name) for lst in nf.list_info(b) for i in lst.items if i.simple}


def _dir_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in dotfiles.IGNORED]
        for f in files:
            p = Path(root) / f
            if not p.is_symlink():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return total


def _adopt(ctx: Context, path: Path) -> Callable[[Session], None]:
    def fix(session: Session) -> None:
        dotfiles.plan_add(ctx, session, str(path))

    return fix


def _relocate(ctx: Context, target: Path) -> Callable[[Session], None]:
    def fix(session: Session) -> None:
        dotfiles.plan_relocate(ctx, session, target)

    return fix


def _relocations(ctx: Context, session: Session) -> list[Suggestion]:
    """Dotfiles kept in the repository but outside ``dotfiles_dir``."""
    out = []
    for e in dotfiles.entries(session):
        new = dotfiles.relocation_target(ctx, e)
        if new is None or e.source is None:
            continue
        old_rel = e.source.relative_to(ctx.config_dir)
        new_rel = new.relative_to(ctx.config_dir)
        out.append(Suggestion(
            Level.OPTIONAL, f"Move {old_rel} to {new_rel}",
            f"Keeps every dotfile under {ctx.dotfiles_dir.name}/, next to the ones ehm adopts. "
            "Only the location in the repository changes; the content and how it is linked stay the same. "
            "Scripts outside Nix that use the old path need updating.",
            _relocate(ctx, e.target),
        ))
    return out


def _broken_links() -> list[Suggestion]:
    generation = applier.current_generation()
    leaves = applier.home_file_leaves(generation) if generation else set()
    broken = dotfiles.broken_links(generation, leaves)
    if not broken:
        return []
    lines = []
    for path in broken:
        backup = dotfiles.latest_backup_of(path)
        lines.append(f"{ui.tilde(path)}" + (f" (latest backup: {ui.tilde(backup)})" if backup else ""))
    return [Suggestion(
        Level.IMPORTANT, f"{len(broken)} managed file(s) point to files that no longer exist",
        "This usually means files were removed from the repository after applying, for example "
        "uncommitted dotfiles deleted by `git reset --hard`. Affected: " + "; ".join(lines),
    )]


def _dotfile_candidates(ctx: Context, session: Session, packages: set[str]) -> list[Suggestion]:
    home = Path.home()
    config = home / ".config"
    managed = dotfiles.entries(session)
    candidates: list[Path] = []
    names = CONFIG_DIRS | {p.replace("_", "-") for p in packages}
    if config.is_dir():
        for child in sorted(config.iterdir()):
            if child.name in names:
                candidates.append(child)
    candidates += [home / f for f in HOME_FILES]

    out: list[Suggestion] = []
    skipped: list[str] = []
    for path in candidates:
        if path.is_symlink() or not path.exists():
            continue
        if any(e.target == path or path in e.target.parents or e.target in path.parents for e in managed):
            continue
        if ctx.config_dir == path or path in ctx.config_dir.parents:
            continue
        if path.is_dir():
            if dotfiles.entry_links_exist(path):  # partially populated by Home Manager already
                continue
            if _dir_size(path) > MAX_SUGGEST_BYTES:
                continue
            if not any(f for _, _, fs in os.walk(path) for f in fs):
                continue
        secrets = dotfiles.find_secrets(path)
        if secrets:
            # For directories, still offer the individual files that look clean.
            secret_paths = {p for p, _ in secrets}
            clean = [p for p in sorted(path.rglob("*")) if p.is_file() and p not in secret_paths] if path.is_dir() else []
            if clean and len(clean) <= 3:
                for p in clean:
                    out.append(Suggestion(
                        Level.RECOMMENDED, f"Move {ui.tilde(p)} into the repository",
                        f"{', '.join(s.name for s in secret_paths)} in the same directory "
                        "look like secrets and were excluded.",
                        _adopt(ctx, p),
                    ))
            else:
                skipped.append(ui.tilde(path))
            continue
        out.append(Suggestion(
            Level.RECOMMENDED, f"Move {ui.tilde(path)} into the repository",
            "Share the same setup across machines with history in git. Edits take effect immediately.",
            _adopt(ctx, path),
        ))
    if skipped:
        out.append(Suggestion(
            Level.INFO, f"Not suggested because they may contain secrets: {', '.join(skipped)}",
            "Move tokens and passwords into a separate file to be able to version these safely.",
        ))
    return out


def analyze(ctx: Context, session: Session) -> list[Suggestion]:
    out: list[Suggestion] = []
    packages = session.package_names()
    enabled = session.enabled_programs()

    # 1. Files invisible to the flake: a common cause of broken builds.
    if ctx.is_flake:
        untracked = untracked_files(ctx)
        nix_files = [f for f in untracked if f.endswith(".nix")]
        if nix_files:
            def fix_track(s: Session, files=tuple(nix_files)) -> None:
                s.notes.append("git add: " + ", ".join(files))
                s.after_write.append(lambda: git_track(ctx, *[ctx.config_dir / f for f in files]))

            out.append(Suggestion(
                Level.IMPORTANT, f".nix files not added to git: {', '.join(nix_files)}",
                "Flakes only see files tracked by git; builds that use these files will fail.",
                fix_track,
            ))
        others = [f for f in untracked if not f.endswith(".nix")]
        if others:
            shown = ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")
            out.append(Suggestion(
                Level.INFO, f"Files not tracked by git: {shown}",
                "If the configuration uses them, `git add` them yourself after checking for secrets.",
            ))

    # 2. Installed both through programs.X and home.packages.
    for name in sorted(packages & enabled):
        out.append(Suggestion(
            Level.RECOMMENDED, f"Remove {name} from home.packages (duplicates programs.{name})",
            f"programs.{name}.enable already installs the package.",
            _remove_package(name),
        ))

    # 3. Packages that have a dedicated Home Manager module.
    modules: list[str] = []
    if ctx.is_flake:
        try:
            modules = ctx.program_modules()
        except ui.EhmError as e:
            ui.warn(f"Could not list program modules; skipping part of the analysis: {e}")
    for name in sorted((packages & set(modules)) - enabled):
        note = PROGRAM_NOTES.get(name, "Its configuration can be managed in Nix as well.")
        out.append(Suggestion(
            Level.OPTIONAL, f"Switch {name} to the programs.{name} module",
            f"{note} Leave it as is if you don't plan to use its options.",
            _migrate(name),
        ))

    # 4. ehm itself.
    if ctx.is_flake and not install.is_installed(ctx, session):
        reason = install.problem(ctx, session)
        if reason is None:
            out.append(Suggestion(
                Level.RECOMMENDED, "Install ehm through your configuration",
                f"Adds ehm as a flake input ({install.flake_url(ctx)}) and to home.packages, so `ehm` is "
                "always on your PATH and `ehm update` keeps it current.",
                lambda s: install.plan_install(ctx, s),
            ))
        else:
            out.append(Suggestion(Level.INFO, "ehm is not installed through your configuration", reason))

    # 5. Dotfiles: broken links, unmanaged files, and sources outside dotfiles_dir.
    out += _broken_links()
    out += _dotfile_candidates(ctx, session, packages)
    out += _relocations(ctx, session)

    # 6. Informational notes.
    system = _system_packages(ctx) if ctx.is_nixos else set()
    duplicated = sorted(system & (packages | enabled))
    if duplicated:
        out.append(Suggestion(
            Level.INFO, f"Installed both system-wide and per-user: {', '.join(duplicated)}",
            "Usually harmless, but keeping only one makes it clear which version is used. "
            "Clean up system packages in configuration.nix yourself.",
        ))
    if ctx.mode == "nixos-flake":
        flake = (ctx.config_dir / "flake.nix").read_text()
        if "backupFileExtension" not in flake:
            out.append(Suggestion(
                Level.INFO, "home-manager.backupFileExtension is not set",
                "ehm backs up conflicting files itself, but running nixos-rebuild directly will fail "
                "Home Manager activation when a file is in the way. Add "
                'home-manager.backupFileExtension = "backup"; to the home-manager settings in flake.nix.',
            ))
    return sorted(out, key=lambda s: s.level)


# ---- curated program bundles ------------------------------------------
BUNDLES: dict[str, dict[str, str]] = {
    "Terminal tools": {
        "bat": "cat with syntax highlighting",
        "eza": "a modern ls",
        "fzf": "fuzzy finder for anything",
        "zoxide": "jump to frequently used directories",
        "btop": "system monitor",
        "yazi": "terminal file manager",
    },
    "Shell": {
        "zsh": "zsh managed by Home Manager (replaces ~/.zshrc)",
        "starship": "shell prompt",
        "atuin": "searchable shell history",
    },
    "Development": {
        "direnv": "per-directory development environments",
        "lazygit": "terminal UI for git",
        "gh": "GitHub CLI",
        "jq": "JSON processor",
    },
}
