"""Build, preview, conflict handling, switching, updates and rollback."""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import generations, ui
from .context import NIX, Context, last_error, xdg_dir
from .dotfiles import (
    entry_links_exist,
    is_managed,
    new_backup_dir,
    process_pending_after_switch,
)
from .workspace import git, git_track, is_git_repo, load_pending, untracked_files


def build_generation(ctx: Context) -> Path:
    """Build only the Home Manager generation (no sudo) to catch errors before switching."""
    ui.step("Building configuration (nothing is applied yet)")
    r = subprocess.run(
        [*NIX, "build", "--no-link", "--print-out-paths", ctx.activation_attr()],
        stdout=subprocess.PIPE, text=True,
    )
    if r.returncode != 0:
        raise ui.EhmError("Build failed; see the errors above. Your system was not changed.")
    return Path(r.stdout.strip().splitlines()[-1])


def current_generation() -> Path | None:
    link = xdg_dir("XDG_STATE_HOME", ".local/state") / "home-manager/gcroots/current-home"
    return link.resolve() if link.exists() else None


def home_file_leaves(generation: Path) -> set[str]:
    """Paths, relative to ``$HOME``, of the files a generation creates."""
    root = generation / "home-files"
    out: set[str] = set()
    if not root.exists():
        return out
    for cur, dirs, files in os.walk(root):
        base = Path(cur)
        for d in list(dirs):
            if (base / d).is_symlink():  # the whole directory is a single link
                dirs.remove(d)
                out.add(str((base / d).relative_to(root)))
        for f in files:
            out.add(str((base / f).relative_to(root)))
    return out


def find_conflicts(generation: Path) -> list[Path]:
    """Existing unmanaged files that Home Manager would refuse to overwrite."""
    home = Path.home()
    conflicts = []
    for rel in sorted(home_file_leaves(generation)):
        target = home / rel
        if not (target.exists() or target.is_symlink()) or is_managed(target):
            continue
        if any(is_managed(p) for p in target.parents if p != home and home in p.parents):
            continue
        conflicts.append(target)
    return conflicts


def show_changes(old: Path | None, new: Path) -> None:
    if old is None:
        ui.info("No previous generation found; skipping the comparison.")
        return
    if old == new:
        ui.ok("No Home Manager changes.")
        return
    ui.step("Changes")
    before, after = home_file_leaves(old), home_file_leaves(new)
    for rel in sorted(after - before):
        print(ui.paint(ui.GREEN, f"  + ~/{rel}"))
    for rel in sorted(before - after):
        print(ui.paint(ui.RED, f"  - ~/{rel}"))
    r = subprocess.run([*NIX, "store", "diff-closures", str(old), str(new)], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        text = r.stdout if sys.stdout.isatty() else re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)
        lines = text.strip().splitlines()
        for line in lines[:40]:
            print("  " + line)
        if len(lines) > 40:
            ui.info(ui.dim(f"... and {len(lines) - 40} more"))


def move_aside(conflicts: list[Path]) -> list[tuple[Path, Path]]:
    """Move conflicting files to a backup directory instead of deleting them."""
    backup_root = new_backup_dir()
    home = Path.home()
    moved = []
    for target in conflicts:
        dst = backup_root / target.relative_to(home)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(dst))
        moved.append((target, dst))
    if moved:
        (backup_root / "manifest.json").write_text(
            json.dumps([{"target": str(a), "backup": str(b)} for a, b in moved], ensure_ascii=False, indent=2)
        )
        ui.info(f"Backed up to {ui.tilde(backup_root)}")
    return moved


def restore(moved: list[tuple[Path, Path]]) -> None:
    for target, backup in moved:
        if not (target.exists() or target.is_symlink()):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(target))


def hm_command(ctx: Context) -> list[str]:
    if shutil.which("home-manager"):
        return ["home-manager"]
    if ctx.is_flake:
        return [*NIX, "run", *ctx.inputs_args(), "home-manager", "--"]
    raise ui.EhmError("The home-manager command was not found.")


def switch_command(ctx: Context) -> list[str]:
    target = f"{ctx.config_dir}#{ctx.host}"
    if ctx.mode == "nixos-flake":
        return ["sudo", "nixos-rebuild", "switch", "--flake", target, *ctx.extra_args]
    if ctx.mode == "nixos-channel":
        return ["sudo", "nixos-rebuild", "switch", *ctx.extra_args]
    if ctx.mode == "hm-flake":
        return [*hm_command(ctx), "switch", "--flake", target, *ctx.extra_args]
    return [*hm_command(ctx), "switch", *ctx.extra_args]


def _adopted_paths(ctx: Context) -> list[Path]:
    return [Path(p["target"]) for p in load_pending(ctx) if p["action"] == "adopt"]


def _warn_changed_originals(ctx: Context, conflicts: list[Path]) -> None:
    """Warn if an original changed after `dot add`; that content will survive only in the backup."""
    for item in load_pending(ctx):
        if item["action"] != "adopt":
            continue
        target, source = Path(item["target"]), Path(item["source"])
        for conflict in conflicts:
            if conflict != target and target not in conflict.parents:
                continue
            copy = source / conflict.relative_to(target) if conflict != target else source
            if conflict.is_file() and (not copy.is_file() or not filecmp.cmp(conflict, copy, shallow=False)):
                ui.warn(f"{ui.tilde(conflict)} differs from the repository copy; its latest content will be kept in the backup.")


def _resolve_conflicts(ctx: Context, conflicts: list[Path], backup_conflicts: bool) -> list[Path]:
    """Decide which conflicts may be moved aside; abort without permission."""
    adopted = _adopted_paths(ctx)
    expected = [c for c in conflicts if any(c == a or a in c.parents for a in adopted)]
    others = [c for c in conflicts if c not in expected]
    if expected:
        ui.info("Originals of adopted files will be moved to the backup directory before switching:")
        for c in expected:
            ui.info(f"  {ui.tilde(c)}")
        _warn_changed_originals(ctx, expected)
    if others:
        ui.warn("Files already exist where Home Manager wants to create its own:")
        for c in others:
            ui.info(f"  {ui.tilde(c)}")
        allowed = backup_conflicts or (not ui.ASSUME_YES and ui.confirm("Move these files to the backup directory and continue?"))
        if not allowed:
            raise ui.EhmError(
                "Aborted: unresolved file conflicts.\n"
                "Use `ehm dot add <file>` to move an existing file into the repository, "
                "or `ehm apply --backup-conflicts` to back them up and continue."
            )
    return conflicts


def check(ctx: Context) -> None:
    """Build and preview the changes without applying them."""
    if not ctx.is_flake:
        raise ui.EhmError("Previews require a flake-based configuration. Use `ehm apply` instead.")
    warn_untracked(ctx)
    new = build_generation(ctx)
    show_changes(current_generation(), new)
    conflicts = find_conflicts(new)
    if conflicts:
        ui.warn(f"{len(conflicts)} conflicting file(s) will need handling on apply:")
        for c in conflicts:
            ui.info(f"  {ui.tilde(c)}")
    ui.ok("Build succeeded. Run `ehm apply` to apply it.")


def warn_untracked(ctx: Context) -> None:
    if not ctx.is_flake:
        return
    nix_files = [f for f in untracked_files(ctx) if f.endswith(".nix")]
    if nix_files:
        ui.warn("Untracked .nix files are invisible to the flake: " + ", ".join(nix_files))


def apply(ctx: Context, *, backup_conflicts: bool = False, commit: bool = False, push: bool = False) -> None:
    """Build, preview, resolve conflicts, then switch to the new generation."""
    warn_untracked(ctx)
    to_move: list[Path] = []
    new: Path | None = None
    if ctx.is_flake:
        new = build_generation(ctx)
        show_changes(current_generation(), new)
        to_move = _resolve_conflicts(ctx, find_conflicts(new), backup_conflicts)
    else:
        ui.warn("Channel-based setup: skipping the pre-build and conflict check.")

    if ctx.is_nixos:
        ui.info("Home Manager runs as a NixOS module, so the whole system is rebuilt (requires sudo).")
    cmd = switch_command(ctx)
    ui.info(ui.dim("Running: " + " ".join(cmd)))
    if not ui.confirm("Apply?", default=True):
        ui.info("Not applied.")
        return

    moved = move_aside(to_move)
    ui.step("Applying")
    try:
        code = subprocess.run(cmd, cwd=ctx.config_dir).returncode
    except KeyboardInterrupt:
        code = 130
    if code != 0:
        restore(moved)
        if moved:
            ui.info("Moved files were restored to their original locations.")
        raise ui.EhmError(f"Apply failed (exit code {code}).")

    if new is not None and ctx.is_nixos:
        _ensure_activated(ctx, new, [t for t, _ in moved])
    process_pending_after_switch(ctx)
    _restore_missing(moved)
    if new is not None:
        generations.record_snapshot(ctx, new)
    ui.ok("Applied")
    if commit or ctx.settings.get("auto_commit"):
        commit_changes(ctx, push or bool(ctx.settings.get("auto_push")))


def _ensure_activated(ctx: Context, generation: Path, moved: list[Path]) -> None:
    """Run the Home Manager activation if the switch did not.

    NixOS only restarts ``home-manager-<user>.service`` when the Home Manager
    generation changed. If ehm moved files aside for a generation that was already
    active (e.g. re-adopting the same dotfiles), their links would never be created.
    """
    expected = moved + [Path(p["target"]) for p in load_pending(ctx) if p["action"] == "adopt"]
    if all(entry_links_exist(t) for t in expected):
        return
    ui.info("Home Manager was not re-activated by the switch; activating it directly.")
    # Same invocation as the NixOS service: driver version 1 leaves the Nix profile alone.
    code = subprocess.run([str(generation / "activate"), "--driver-version", "1"]).returncode
    if code != 0:
        ui.warn(f"Home Manager activation failed (exit code {code}).")


def _restore_missing(moved: list[tuple[Path, Path]]) -> None:
    """Never leave a hole: put back moved files whose place is still empty."""
    missing = [(t, b) for t, b in moved if not (t.exists() or t.is_symlink())]
    if missing:
        restore(missing)
        for target, _ in missing:
            ui.warn(f"{ui.tilde(target)} was not linked, so the original was put back.")


def commit_changes(ctx: Context, push: bool) -> None:
    if not is_git_repo(ctx):
        return
    git(ctx, "add", "-u")
    if ctx.dotfiles_dir.exists():
        git_track(ctx, ctx.dotfiles_dir)
    if git(ctx, "diff", "--cached", "--quiet", check=False).returncode == 0:
        ui.info("Nothing to commit.")
        return
    git(ctx, "commit", "-m", f"ehm: apply @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    ui.ok("Committed")
    if push:
        r = git(ctx, "push", check=False)
        if r.returncode == 0:
            ui.ok("Pushed")
        else:
            ui.warn("Push failed:\n" + last_error(r.stderr))


def update(ctx: Context, inputs: list[str], *, do_apply: bool = True, backup_conflicts: bool = False) -> None:
    ui.step("Checking for updates")
    if ctx.is_flake:
        code = subprocess.run([*NIX, "flake", "update", *inputs], cwd=ctx.config_dir).returncode
    else:
        code = subprocess.run(["nix-channel", "--update"]).returncode
    if code != 0:
        raise ui.EhmError("Update failed.")
    if ctx.is_flake and is_git_repo(ctx):
        if not git(ctx, "diff", "--quiet", "--", "flake.lock", check=False).returncode:
            ui.ok("Already up to date.")
            return
    if do_apply:
        try:
            apply(ctx, backup_conflicts=backup_conflicts)
        except ui.EhmError:
            ui.warn("To revert the update, run `git checkout flake.lock` in the configuration directory.")
            raise


def collect_garbage(ctx: Context, older_than: str) -> None:
    cmd = ["nix-collect-garbage", "--delete-older-than", older_than]
    if ctx.is_nixos:
        cmd.insert(0, "sudo")
    ui.warn(f"Deleting generations older than {older_than}. You will not be able to roll back to them.")
    ui.info(ui.dim("Running: " + " ".join(cmd)))
    if ui.confirm("Continue?"):
        if subprocess.run(cmd).returncode != 0:
            raise ui.EhmError("Garbage collection failed.")
        ui.ok("Garbage collected")
