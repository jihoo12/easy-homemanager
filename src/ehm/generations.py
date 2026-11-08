"""Generations: listing, rolling back, and recovering the configuration each was built from.

A generation's store path does not contain the Nix source it was built from, so the
configuration is recovered from one of two places:

* an exact snapshot that ``ehm apply`` records for every generation it builds, or
* for older generations, the last git commit of the configuration repository made
  before the generation was created (an approximation: the tree may have been dirty).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import ui
from .context import Context, state_dir, xdg_dir
from .nixfile import NixFile, NixParseError
from .workspace import Session, git, is_git_repo

SYSTEM_PROFILE = Path("/nix/var/nix/profiles/system")


@dataclass
class Generation:
    number: int
    link: Path  # the profile's `<name>-<number>-link`
    created: datetime
    current: bool
    home: Path | None  # the Home Manager generation it contains

    @property
    def label(self) -> str:
        return f"#{self.number}  {self.created:%Y-%m-%d %H:%M}"


@dataclass
class ConfigSnapshot:
    files: dict[str, str]  # path relative to the configuration directory → content
    origin: str
    exact: bool


# ---- listing --------------------------------------------------------------


def _profile(ctx: Context) -> Path | None:
    if ctx.is_nixos:
        return SYSTEM_PROFILE
    user = os.environ.get("USER") or ctx.user
    for candidate in (
        xdg_dir("XDG_STATE_HOME", ".local/state") / "nix/profiles/home-manager",
        Path("/nix/var/nix/profiles/per-user") / user / "home-manager",
    ):
        if candidate.is_symlink():
            return candidate
    return None


def _home_generation(ctx: Context, link: Path) -> Path | None:
    if not ctx.is_nixos:
        return link.resolve()
    unit = link / f"etc/systemd/system/home-manager-{ctx.user}.service"
    try:
        text = unit.read_text()
    except OSError:
        return None
    m = re.search(r"^ExecStart=.*\s(/nix/store/\S+-home-manager-generation)\s*$", text, re.M)
    return Path(m.group(1)) if m else None


def list_generations(ctx: Context) -> list[Generation]:
    """All generations of the system (NixOS module) or Home Manager profile, newest first."""
    profile = _profile(ctx)
    if profile is None:
        return []
    current = os.readlink(profile) if profile.is_symlink() else ""
    pattern = re.compile(rf"^{re.escape(profile.name)}-(\d+)-link$")
    out = []
    for link in profile.parent.iterdir():
        m = pattern.match(link.name)
        if not m:
            continue
        out.append(Generation(
            number=int(m.group(1)),
            link=link,
            created=datetime.fromtimestamp(link.lstat().st_mtime),
            current=link.name == current,
            home=_home_generation(ctx, link),
        ))
    return sorted(out, key=lambda g: g.number, reverse=True)


# ---- configuration snapshots ------------------------------------------------


def _snapshot_path(home_generation: Path) -> Path:
    return state_dir() / "snapshots" / f"{home_generation.name}.json"


def _rel(ctx: Context, path: Path) -> str:
    try:
        return str(path.relative_to(ctx.config_dir))
    except ValueError:
        return str(path)


def record_snapshot(ctx: Context, home_generation: Path) -> None:
    """Remember which configuration files built ``home_generation``."""
    paths = [*Session(ctx).module_files(), ctx.config_dir / "flake.nix"]
    files = {_rel(ctx, p): p.read_text() for p in dict.fromkeys(paths) if p.is_file()}
    target = _snapshot_path(home_generation)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"generation": str(home_generation), "files": files}, ensure_ascii=False))


def config_of(ctx: Context, generation: Generation) -> ConfigSnapshot | None:
    """The configuration a generation was built from, exact if ehm recorded it."""
    if generation.home is not None:
        snapshot = _snapshot_path(generation.home)
        if snapshot.is_file():
            data = json.loads(snapshot.read_text())
            return ConfigSnapshot(data["files"], "Recorded by ehm when this generation was applied", True)
    return _config_from_git(ctx, generation)


def _config_from_git(ctx: Context, generation: Generation) -> ConfigSnapshot | None:
    if not is_git_repo(ctx):
        return None
    try:
        return _git_snapshot(ctx, generation)
    except ui.EhmError:  # e.g. git not installed
        return None


def _git_snapshot(ctx: Context, generation: Generation) -> ConfigSnapshot | None:
    r = git(ctx, "log", "-1", f"--before={generation.created.isoformat()}", "--format=%H%x00%ci%x00%s", check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    commit, date, subject = r.stdout.strip().split("\x00", 2)
    files = {}
    for path in [*Session(ctx).module_files(), ctx.config_dir / "flake.nix"]:
        rel = _rel(ctx, path)
        shown = git(ctx, "show", f"{commit}:{rel}", check=False)
        if shown.returncode == 0:
            files[rel] = shown.stdout
    if not files:
        return None
    origin = (
        f"Approximate: git commit {commit[:8]} ({date[:16]}, “{subject}”), the last commit before "
        "this generation. Uncommitted changes at the time are not included."
    )
    return ConfigSnapshot(files, origin, False)


def diff_against_current(ctx: Context, snapshot: ConfigSnapshot, label: str = "snapshot") -> list[str]:
    """Unified diff from the current files to the snapshot (what restoring would change)."""
    lines: list[str] = []
    for rel, text in snapshot.files.items():
        path = ctx.config_dir / rel
        current = path.read_text() if path.is_file() else ""
        lines += ui.unified_diff(path, current, text, ("current", label))
    return lines


def plan_restore(ctx: Context, session: Session, snapshot: ConfigSnapshot) -> list[str]:
    """Stage replacing the current configuration files with the snapshot's.

    Returns the files that were skipped because they no longer exist.
    """
    skipped = []
    for rel, text in snapshot.files.items():
        path = ctx.config_dir / rel
        if not path.is_file():
            skipped.append(rel)
            continue
        session.load(path)
        try:
            session.save(NixFile(text, path))
        except NixParseError as e:
            raise ui.EhmError(f"The snapshot of {rel} could not be parsed: {e}") from e
    if skipped:
        session.notes.append("Not restored (no longer exist): " + ", ".join(skipped))
    return skipped


# ---- rollback ---------------------------------------------------------------


def rollback_commands(ctx: Context, generation: Generation) -> list[list[str]]:
    if ctx.is_nixos:
        return [
            ["sudo", "nix-env", "-p", str(SYSTEM_PROFILE), "--switch-generation", str(generation.number)],
            ["sudo", str(SYSTEM_PROFILE / "bin/switch-to-configuration"), "switch"],
        ]
    # A Home Manager generation's activation script also points the profile at it.
    return [[str(generation.link / "activate")]]


def rollback_to(ctx: Context, generation: Generation, *, ask: bool = True) -> None:
    """Switch to ``generation``. The configuration files are left as they are."""
    if generation.current:
        raise ui.EhmError(f"Generation {generation.number} is already active.")
    commands = rollback_commands(ctx, generation)
    if ctx.is_nixos:
        ui.warn("Home Manager runs as a NixOS module, so the whole system rolls back.")
    for cmd in commands:
        ui.info(ui.dim("Running: " + " ".join(cmd)))
    if ask and not ui.confirm(f"Switch to generation {generation.number} ({generation.created:%Y-%m-%d %H:%M})?"):
        return
    for cmd in commands:
        if subprocess.run(cmd).returncode != 0:
            raise ui.EhmError("Rollback failed.")
    ui.ok(f"Switched to generation {generation.number}")
    ui.warn(
        f"The files in {ui.tilde(ctx.config_dir)} are unchanged, so the next apply builds them again. "
        "To keep this generation's settings, restore its configuration (`ehm generations "
        f"{generation.number} --restore`)."
    )


def find(ctx: Context, number: int | None) -> Generation:
    """Generation ``number``, or the one before the current generation."""
    gens = list_generations(ctx)
    if not gens:
        raise ui.EhmError("No generations found.")
    if number is not None:
        for g in gens:
            if g.number == number:
                return g
        raise ui.EhmError(f"Generation {number} does not exist.")
    current = next((i for i, g in enumerate(gens) if g.current), 0)
    if current + 1 >= len(gens):
        raise ui.EhmError("There is no previous generation to roll back to.")
    return gens[current + 1]
