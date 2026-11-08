"""Committing and pushing the configuration repository.

``commit`` includes changes to tracked files, everything under ``dotfiles_dir`` and
new ``.nix`` files (flakes cannot see untracked files anyway). Other untracked files
are left out because they might contain secrets; :func:`status` lists them so the user
can add them deliberately.

``git commit`` and ``git push`` run attached to the terminal, so SSH passphrases,
credential helpers and commit signing can prompt.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from . import ui
from .context import Context
from .workspace import git, is_git_repo


@dataclass
class RepoStatus:
    to_commit: list[str] = field(default_factory=list)
    # The subset of ``to_commit`` that still needs `git add`: paths with unstaged changes or
    # new files. Already staged changes (e.g. `git mv`, `git rm`) are committed as they are;
    # their old paths no longer exist, so passing them to `git add` would fail.
    to_stage: list[str] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)  # untracked files not included
    has_remote: bool = False
    ahead: int | None = None  # commits not pushed yet; None without an upstream branch


def status(ctx: Context) -> RepoStatus:
    if not is_git_repo(ctx):
        raise ui.EhmError(f"{ui.tilde(ctx.config_dir)} is not a git repository.")
    out = RepoStatus()
    dotfiles = ctx.dotfiles_dir.relative_to(ctx.config_dir).as_posix().rstrip("/") + "/"
    entries = git(ctx, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            i += 1  # the next field is the rename/copy source, which is already staged
        if code == "??" and not (path.endswith(".nix") or path.startswith(dotfiles)):
            out.left_out.append(path)
            continue
        out.to_commit.append(path)
        if code == "??" or code[1] != " ":
            out.to_stage.append(path)
    out.has_remote = bool(git(ctx, "remote").stdout.strip())
    r = git(ctx, "rev-list", "--count", "@{upstream}..HEAD", check=False)
    out.ahead = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
    return out


def default_message(ctx: Context, st: RepoStatus) -> str:
    dotfiles = ctx.dotfiles_dir.relative_to(ctx.config_dir).as_posix().rstrip("/") + "/"
    dot = [p for p in st.to_commit if p.startswith(dotfiles)]
    others = sorted(p for p in st.to_commit if p not in dot)
    parts = others[:3] + ([f"{len(others) - 3} more"] if len(others) > 3 else [])
    if dot:
        parts.append(f"{len(dot)} dotfile{'s' if len(dot) != 1 else ''}")
    return "ehm: update " + ", ".join(parts) if parts else "ehm: update configuration"


def _run(ctx: Context, *args: str) -> None:
    """Run git attached to the terminal so that it can prompt."""
    try:
        code = subprocess.run(["git", "-C", str(ctx.config_dir), *args]).returncode
    except FileNotFoundError:
        raise ui.EhmError("git is not installed.") from None
    if code != 0:
        raise ui.EhmError(f"`git {args[0]}` failed (exit code {code}).")


def commit(ctx: Context, message: str | None = None, *, push: bool = False) -> None:
    """Commit the configuration (see the module docstring), then optionally push."""
    st = status(ctx)
    if st.left_out:
        ui.info(ui.dim("Not included (untracked, not .nix or dotfiles): " + ", ".join(st.left_out)))
    if st.to_commit:
        if st.to_stage:
            git(ctx, "add", "-A", "--", *st.to_stage)
        _run(ctx, "commit", "-m", message or default_message(ctx, st))
        ui.ok("Committed")
    else:
        ui.info("Nothing to commit.")
    if push:
        push_changes(ctx)


def push_changes(ctx: Context) -> None:
    if not git(ctx, "remote").stdout.strip():
        raise ui.EhmError("The repository has no remote to push to.")
    _run(ctx, "push")
    ui.ok("Pushed")
