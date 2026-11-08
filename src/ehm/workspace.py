"""Editing sessions over a set of Nix files.

All modifications are staged in a :class:`Session`, shown to the user as a diff,
and written together only after confirmation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import ui
from .context import NIX, Context, last_error, state_dir
from .nixfile import NixFile, NixParseError, normalize_pkg

PACKAGES = ("home", "packages")


class Session:
    """Collects changes across several ``.nix`` files and writes them on confirmation."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.originals: dict[Path, str] = {}
        self.current: dict[Path, NixFile] = {}
        self.notes: list[str] = []
        self.before_write: list[Callable[[], None]] = []
        self.after_write: list[Callable[[], None]] = []
        # Reverts side effects if post-save verification fails.
        self.undo: list[Callable[[], None]] = []
        self.last_snapshot: dict[Path, str] = {}

    def load(self, path: Path) -> NixFile:
        if path not in self.current:
            text = path.read_text()
            self.originals[path] = text
            try:
                self.current[path] = NixFile(text, path)
            except NixParseError as e:
                raise ui.EhmError(f"Could not parse {path}: {e}") from e
        return self.current[path]

    def save(self, nf: NixFile) -> None:
        self.current[nf.path] = nf

    # ---- module discovery --------------------------------------------
    def module_files(self) -> list[Path]:
        """The home file plus every file it imports through relative paths."""
        seen: list[Path] = []
        queue = [self.ctx.home_file]
        while queue:
            path = queue.pop(0)
            if path in seen or not path.exists():
                continue
            seen.append(path)
            try:
                nf = self.load(path)
            except ui.EhmError as e:
                ui.warn(str(e))
                continue
            for rel in nf.import_paths():
                target = (path.parent / rel).resolve()
                if target.is_dir():
                    target = target / "default.nix"
                queue.append(target)
        return seen

    def modules(self) -> list[NixFile]:
        return [self.load(p) for p in self.module_files() if p in self.current or p.exists()]

    # ---- queries -----------------------------------------------------
    def packages(self) -> list[tuple[NixFile, str, bool]]:
        """``(file, item, is_simple)`` for every entry of ``home.packages``."""
        out = []
        for nf in self.modules():
            b = nf.find(PACKAGES)
            if b is None:
                continue
            for lst in nf.list_info(b):
                for it in lst.items:
                    out.append((nf, normalize_pkg(it.name) if it.simple else it.name, it.simple))
        return out

    def package_names(self) -> set[str]:
        return {name for _, name, simple in self.packages() if simple}

    def enabled_programs(self) -> set[str]:
        """Names ``X`` for which ``programs.X.enable = true`` is set literally."""
        out = set()
        for nf in self.modules():
            for b in nf.bindings:
                if len(b.path) == 3 and b.path[0] == "programs" and b.path[2] == "enable":
                    if nf.value_text(b) == "true":
                        out.add(b.path[1])
        return out

    def file_with_packages(self) -> NixFile:
        for nf in self.modules():
            if nf.find(PACKAGES) is not None:
                return nf
        return self.load(self.ctx.home_file)

    def file_with_path(self, path: tuple[str, ...]) -> NixFile | None:
        """The file that defines ``path`` or its closest defined ancestor."""
        for n in range(len(path), 0, -1):
            for nf in self.modules():
                if nf.find(path[:n]) is not None:
                    return nf
        return None

    # ---- edits -------------------------------------------------------
    def remove_package(self, name: str) -> bool:
        """Remove ``name`` from ``home.packages`` in the first module that lists it."""
        for nf in self.modules():
            try:
                self.save(nf.remove_list_item(PACKAGES, name))
                return True
            except KeyError:
                continue
        return False

    # ---- saving ------------------------------------------------------
    def changed_files(self) -> list[Path]:
        return [p for p, nf in self.current.items() if nf.text != self.originals[p]]

    def has_changes(self) -> bool:
        return bool(self.changed_files() or self.notes or self.before_write or self.after_write)

    def diffs(self) -> list[tuple[Path, list[str]]]:
        """Unified diff lines for every changed file."""
        return [
            (path, ui.unified_diff(path, self.originals[path], self.current[path].text))
            for path in self.changed_files()
        ]

    def show(self) -> None:
        for path in self.changed_files():
            ui.show_diff(path, self.originals[path], self.current[path].text)
        for note in self.notes:
            ui.info(note)

    def validate(self) -> None:
        """Refuse to write files that ``nix-instantiate --parse`` rejects."""
        if shutil.which("nix-instantiate") is None:
            return
        for path in self.changed_files():
            with tempfile.NamedTemporaryFile("w", suffix=".nix", delete=False) as f:
                f.write(self.current[path].text)
                tmp = f.name
            try:
                r = subprocess.run(
                    ["nix-instantiate", "--parse", tmp], capture_output=True, text=True
                )
                if r.returncode != 0:
                    raise ui.EhmError(
                        f"The edited {path} is not valid Nix, so nothing was saved:\n"
                        + last_error(r.stderr)
                        + "\nThis is likely a bug in ehm. Please edit the file manually with `ehm edit`."
                    )
            finally:
                os.unlink(tmp)

    def write(self) -> dict[Path, str]:
        """Run the hooks and write all changed files, without asking.

        Returns the previous contents of the written files, for :meth:`revert`.
        """
        snapshot = {p: self.originals[p] for p in self.changed_files()}
        for fn in self.before_write:
            fn()
        for path in snapshot:
            write_atomic(path, self.current[path].text)
        for fn in self.after_write:
            fn()
        for path in list(self.current):
            self.originals[path] = self.current[path].text
        self.notes.clear()
        self.before_write.clear()
        self.after_write.clear()
        return snapshot

    def revert(self, snapshot: dict[Path, str]) -> None:
        """Undo a :meth:`write`: restore file contents and roll back side effects."""
        for path, text in snapshot.items():
            write_atomic(path, text)
            self.originals[path] = text
            self.current.pop(path, None)
        for undo in self.undo:
            undo()
        self.undo.clear()

    def commit(self, question: str = "Save these changes?") -> bool:
        """Show the diff, ask for confirmation and write. Returns ``True`` if saved."""
        if not self.has_changes():
            ui.info("Nothing to change.")
            return False
        self.show()
        self.validate()
        if not ui.confirm(question, default=True):
            ui.info("Not saved.")
            return False
        self.last_snapshot = self.write()
        return True


def evaluate(ctx: Context) -> str | None:
    """Evaluate the Home Manager configuration. Returns the error text, or ``None`` if it is fine.

    Channel-based setups cannot be evaluated this way and always pass.
    """
    if not ctx.is_flake:
        return None
    r = subprocess.run(
        [*NIX, "eval", "--raw", f"{ctx.activation_attr()}.drvPath"], capture_output=True, text=True
    )
    return None if r.returncode == 0 else last_error(r.stderr)


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---- pending operations ----------------------------------------------
# Adopting or releasing a dotfile only moves files on the next successful
# switch, so the intent is recorded here until then.


def _pending_path(ctx: Context) -> Path:
    key = str(ctx.config_dir).strip("/").replace("/", "_") or "root"
    return state_dir() / f"pending-{key}.json"


def load_pending(ctx: Context) -> list[dict]:
    path = _pending_path(ctx)
    if not path.exists():
        return []
    return json.loads(path.read_text())


def save_pending(ctx: Context, items: list[dict]) -> None:
    path = _pending_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2))


# ---- git -------------------------------------------------------------


def is_git_repo(ctx: Context) -> bool:
    return (ctx.config_dir / ".git").exists()


def git(ctx: Context, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["git", "-C", str(ctx.config_dir), *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise ui.EhmError("git is not installed.") from None
    if check and r.returncode != 0:
        raise ui.EhmError(f"`git {' '.join(args)}` failed:\n" + last_error(r.stderr))
    return r


def git_track(ctx: Context, *paths: Path) -> None:
    """Stage new files: flakes only see files that git tracks."""
    if is_git_repo(ctx):
        git(ctx, "add", "-A", "--", *[str(p) for p in paths])


def untracked_files(ctx: Context) -> list[str]:
    if not is_git_repo(ctx):
        return []
    r = git(ctx, "ls-files", "--others", "--exclude-standard")
    return [line for line in r.stdout.splitlines() if line]
