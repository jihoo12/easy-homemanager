"""Move dotfiles into the configuration repository and let Home Manager manage them."""

from __future__ import annotations

import enum
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import ui
from .context import Context, state_dir
from .nixfile import NixFile, NixParseError, nix_string, relative_nix_path, tokenize
from .workspace import Session, git, git_track, is_git_repo, load_pending, save_pending

KINDS = {("home", "file"): "", ("xdg", "configFile"): ".config/", ("xdg", "dataFile"): ".local/share/"}
LINK_RE = re.compile(r'mkOutOfStoreSymlink\s+"([^"$]+)"')
IGNORED = {".git", "__pycache__", "node_modules", ".direnv", ".cache"}
MAX_BYTES = 20 * 1024 * 1024

SECRET_NAMES = re.compile(
    r"^(id_(rsa|dsa|ecdsa|ed25519)(_.*)?|.*\.(pem|key|p12|pfx|gpg|kdbx)|\.?netrc|\.npmrc|\.pypirc"
    r"|\.git-credentials|credentials(\..*)?|hosts\.yml|auth\.json|\.?env(\..*)?"
    r"|.*(secret|token|password).*)$",
    re.I,
)
SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".kube", ".password-store", ".claude", ".codex", ".docker"}
SECRET_CONTENT = [
    (re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY"), "private key"),
    (re.compile(rb"\b(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}"), "GitHub token"),
    (re.compile(rb"\bsk-[A-Za-z0-9_\-]{20,}"), "API key"),
    (re.compile(rb"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(rb"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "Slack token"),
    (
        re.compile(
            rb"(?i)\b[\w\-]*(password|passwd|api[_\-]?key|secret|auth[_\-]?token|access[_\-]?token)[\w\-]*"
            rb"[\"']?\s*[=:]\s*[\"']?[^\s\"'$]{8,}"
        ),
        "value that looks like a password or key",
    ),
]


class Status(enum.Enum):
    PENDING = "pending apply"
    LINKED = "linked"
    CONFLICT = "conflict (a regular file is in the way)"
    NOT_APPLIED = "not applied yet"

    @property
    def color(self) -> str:
        return {Status.LINKED: ui.GREEN, Status.PENDING: ui.YELLOW}.get(self, ui.RED)


@dataclass
class Entry:
    """A ``home.file`` / ``xdg.configFile`` / ``xdg.dataFile`` definition."""

    kind: tuple[str, str]
    name: str
    target: Path
    source_expr: str | None
    source: Path | None  # the backing file inside the repository, if known
    linked: bool  # uses mkOutOfStoreSymlink
    inline: bool  # content given inline via `text = ...`
    nix_file: NixFile


def is_managed(path: Path) -> bool:
    """Whether ``path`` is a symlink created by Home Manager."""
    if not path.is_symlink():
        return False
    link = os.readlink(path)
    return link.startswith("/nix/store/") and "home-manager-files" in link


def entries(session: Session) -> list[Entry]:
    """All dotfile definitions across the session's modules, sorted by target."""
    found: dict[tuple, dict] = {}
    for nf in session.modules():
        for b in nf.bindings:
            if len(b.path) < 3 or b.path[:2] not in KINDS:
                continue
            key = (b.path[:2], b.path[2], nf.path)
            data = found.setdefault(key, {"nf": nf})
            if len(b.path) == 4:
                data[b.path[3]] = nf.value_text(b)
    home = Path.home()
    out = []
    for (kind, name, _), data in found.items():
        nf = data["nf"]
        expr = data.get("source")
        source, linked = None, False
        if expr:
            m = LINK_RE.search(expr)
            if m:
                source, linked = Path(m.group(1)), True
            else:
                simple = expr.strip("()")
                concat = re.match(r'^(\.{1,2}/[^\s]*)\s*\+\s*"(.*)"$', simple)
                if simple.startswith(("./", "../")) and " " not in simple:
                    source = (nf.path.parent / simple).resolve()
                elif concat:
                    source = (nf.path.parent / (concat.group(1) + concat.group(2))).resolve()
        target_rel = KINDS[kind] + name
        if "target" in data:
            target_rel = data["target"].strip('"') if kind == ("home", "file") else KINDS[kind] + data["target"].strip('"')
        out.append(Entry(kind, name, home / target_rel, expr, source, linked, "text" in data, nf))
    return sorted(out, key=lambda e: str(e.target))


def entry_status(entry: Entry, pending: list[dict]) -> Status:
    target = str(entry.target)
    if any(p["target"] == target for p in pending if p["action"] == "adopt"):
        return Status.PENDING
    if is_managed(entry.target):
        return Status.LINKED
    if entry.target.is_dir() and not entry.target.is_symlink():
        # With `recursive = true`, the files inside the directory are linked individually.
        for root, _, files in os.walk(entry.target):
            if any(is_managed(Path(root) / f) for f in files):
                return Status.LINKED
    if entry.target.exists() or entry.target.is_symlink():
        return Status.CONFLICT
    return Status.NOT_APPLIED


def find_secrets(path: Path) -> list[tuple[Path, str]]:
    """Find files that look like they contain secrets. Never returns the values themselves."""
    found: list[tuple[Path, str]] = []
    if path.is_dir() and not path.is_symlink():
        files = (Path(r) / f for r, ds, fs in os.walk(path) for f in fs
                 if not (set(Path(r).parts) & IGNORED))
    else:
        files = [path]
    for f in files:
        if set(f.parts) & SECRET_DIRS:
            found.append((f, "inside a sensitive directory"))
            continue
        if SECRET_NAMES.match(f.name):
            found.append((f, "sensitive file name"))
            continue
        if f.is_symlink() or not f.is_file():
            continue
        try:
            with f.open("rb") as fh:
                data = fh.read(1024 * 1024)
        except OSError:
            continue
        for pattern, label in SECRET_CONTENT:
            if pattern.search(data):
                found.append((f, label))
                break
    return found


def _tree_problems(path: Path) -> tuple[int, list[str]]:
    """Total size, plus entries that cannot be versioned (symlinks pointing outside)."""
    if path.is_file():
        return path.stat().st_size, []
    total = 0
    problems = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in IGNORED]
        for name in files + dirs:
            p = Path(root) / name
            if p.is_symlink():
                resolved = Path(os.path.realpath(p))
                if not str(resolved).startswith(str(path) + os.sep):
                    problems.append(f"{ui.tilde(p)} (symlink pointing outside the directory)")
            elif p.is_file():
                total += p.stat().st_size
    return total, problems


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(
            src, dst, symlinks=True,
            ignore=shutil.ignore_patterns(*IGNORED),
        )
    else:
        shutil.copy2(src, dst)


def resolve_target(raw: str) -> Path:
    """Expand ``~`` and make absolute, without resolving symlinks."""
    return Path(os.path.abspath(os.path.expanduser(raw)))


def plan_add(ctx: Context, session: Session, raw: str, *, copy: bool = False, allow_secrets: bool = False) -> Path:
    """Stage adopting ``raw`` into the repository. Files are copied only once the user confirms."""
    target = resolve_target(raw)
    home = Path.home()
    if not (target.exists() or target.is_symlink()):
        raise ui.EhmError(f"No such file: {ui.tilde(target)}")
    try:
        rel = target.relative_to(home)
    except ValueError:
        raise ui.EhmError(f"Only files inside your home directory can be managed: {target}") from None
    if str(rel) == ".":
        raise ui.EhmError("Cannot add the entire home directory.")
    if target == ctx.config_dir or ctx.config_dir in target.parents:
        raise ui.EhmError("Cannot add files that are inside the configuration repository.")
    if is_managed(target):
        raise ui.EhmError(
            f"{ui.tilde(target)} is already managed by Home Manager.\n"
            "If it is generated by a programs.* module, change it with `ehm configure`."
        )
    if target.is_symlink():
        raise ui.EhmError(
            f"{ui.tilde(target)} is a symlink (to {os.readlink(target)}).\n"
            "Replace it with the file it points to and try again."
        )
    for e in entries(session):
        if e.target == target or e.target in target.parents or target in e.target.parents:
            raise ui.EhmError(f"Overlaps with an already managed path: {ui.tilde(e.target)}")

    secrets = find_secrets(target)
    if secrets and not allow_secrets:
        listing = "\n".join(f"  - {ui.tilde(p)}: {why}" for p, why in secrets[:10])
        raise ui.EhmError(
            "Not added because it appears to contain secrets:\n"
            f"{listing}\n"
            "Dotfiles may end up in the git repository and in the world-readable /nix/store.\n"
            "Move the secrets elsewhere and try again, or pass --allow-secrets if you are sure."
        )
    size, problems = _tree_problems(target)
    if problems:
        raise ui.EhmError("Contains entries that cannot be stored in the repository:\n  " + "\n  ".join(problems[:10]))
    if size > MAX_BYTES:
        raise ui.EhmError(
            f"Too large ({size // (1024 * 1024)} MiB); this may be an application cache or state "
            "directory. Add only the configuration files you need."
        )

    source = ctx.dotfiles_dir / rel
    if source.exists():
        raise ui.EhmError(f"The repository already contains {source}")

    rel_str = str(rel)
    if rel_str.startswith(".config/"):
        kind, name = ("xdg", "configFile"), rel_str[len(".config/"):]
    else:
        kind, name = ("home", "file"), rel_str

    # Prefer the module that already declares dotfiles.
    existing = entries(session)
    nf = existing[-1].nix_file if existing else session.load(ctx.home_file)
    nf.ensure_editable()
    is_dir = target.is_dir()
    path = (*kind, name)
    if copy:
        expr = relative_nix_path(source, nf.path.parent)
        if is_dir:
            nf = nf.set_expr(path, f"{{\n{{INDENT}}  source = {expr};\n{{INDENT}}  recursive = true;\n{{INDENT}}}}")
        else:
            nf = nf.set_expr((*path, "source"), expr)
    else:
        nf = nf.ensure_arg("config")
        expr = f"config.lib.file.mkOutOfStoreSymlink {nix_string(str(source))}"
        nf = nf.set_expr((*path, "source"), expr)
    session.save(nf)

    how = "read-only copy made at build time" if copy else "symlink; edits apply immediately"
    session.notes.append(f"Will copy {ui.tilde(target)} → {ui.tilde(source)}  ({how})")
    if is_git_repo(ctx):
        session.notes.append(
            "The copy is staged in git but not committed. Commit it (`ehm commit`, or m in the TUI): "
            "`git reset --hard` deletes staged files, which would leave the link dangling."
        )

    def do_copy() -> None:
        _copy(target, source)
        git_track(ctx, source)
        items = load_pending(ctx)
        items.append({"action": "adopt", "target": str(target), "source": str(source)})
        save_pending(ctx, items)

    def undo() -> None:
        items = [p for p in load_pending(ctx) if p.get("target") != str(target)]
        save_pending(ctx, items)
        if source.exists():
            discard_source(ctx, source)

    session.before_write.append(do_copy)
    session.undo.append(undo)
    return target


def find_entry(session: Session, query: str) -> Entry:
    """Resolve a user query (path, name or substring) to a single managed entry."""
    all_entries = entries(session)
    target = resolve_target(query)
    for e in all_entries:
        if e.target == target or e.name == query or str(e.target.relative_to(Path.home())) == query:
            return e
    matches = [e for e in all_entries if query in str(e.target)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(ui.tilde(e.target) for e in matches)
        raise ui.EhmError(f"'{query}' matches several entries: {names}")
    raise ui.EhmError(f"No managed dotfile matches '{query}'. See `ehm dot list`.")


def _inside(path: Path | None, parent: Path) -> bool:
    return path is not None and (path == parent or parent in path.parents)


def plan_remove(ctx: Context, session: Session, query: str, *, delete: bool = False) -> None:
    """Stage releasing an entry. By default its content is restored as a regular file."""
    entry = find_entry(session, query)
    if entry.inline and not delete:
        raise ui.EhmError(
            f"{ui.tilde(entry.target)} is defined inline in Nix, so there is no file to restore.\n"
            "Pass --delete to remove it entirely."
        )
    if entry.source is None and not delete:
        raise ui.EhmError(
            f"Cannot determine where {ui.tilde(entry.target)} comes from ({entry.source_expr}).\n"
            "Edit it manually with `ehm edit`, or pass --delete to remove it entirely."
        )
    nf = entry.nix_file.unset((*entry.kind, entry.name))
    session.save(nf)

    pending = load_pending(ctx)
    was_pending = next(
        (p for p in pending if p["action"] == "adopt" and p["target"] == str(entry.target)), None
    )
    owned = _inside(entry.source, ctx.dotfiles_dir)

    if was_pending:
        # Not applied yet: the original is still in place, so only clean up the repository copy.
        session.notes.append(f"Cancel adoption: {ui.tilde(entry.target)} is left untouched.")

        def cancel() -> None:
            items = [p for p in load_pending(ctx) if p != was_pending]
            save_pending(ctx, items)
            if entry.source and entry.source.exists():
                discard_source(ctx, entry.source)

        session.after_write.append(cancel)
        return

    outcome = "be removed" if delete else "become a regular file again"
    session.notes.append(f"On the next apply, {ui.tilde(entry.target)} will {outcome}.")

    def record() -> None:
        items = load_pending(ctx)
        items.append({
            "action": "release",
            "target": str(entry.target),
            "source": str(entry.source) if entry.source else None,
            "keep_file": not delete,
            "discard_source": owned,
        })
        save_pending(ctx, items)

    session.after_write.append(record)


def new_backup_dir() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = state_dir() / "backups" / stamp
    n = 1
    while path.exists():
        path = state_dir() / "backups" / f"{stamp}-{n}"
        n += 1
    return path


def discard_source(ctx: Context, source: Path) -> None:
    """Remove ``source`` from the repository by moving it to the backup directory."""
    backup = new_backup_dir() / "repo" / source.relative_to(ctx.config_dir)
    backup.parent.mkdir(parents=True, exist_ok=True)
    tracked = is_git_repo(ctx)
    shutil.move(str(source), str(backup))
    if tracked:
        git(ctx, "add", "-A", "--", str(source), check=False)
    _prune_empty(source.parent, ctx.dotfiles_dir.parent)


def _prune_empty(directory: Path, stop: Path) -> None:
    """Remove ``directory`` and its parents while they are empty, stopping at ``stop``."""
    while directory != stop and stop in directory.parents and directory.exists() and not any(directory.iterdir()):
        directory.rmdir()
        directory = directory.parent


def _git_record_move(ctx: Context, old: Path, new: Path) -> None:
    if is_git_repo(ctx):
        git(ctx, "add", "-A", "--", str(old), check=False)  # may never have been tracked
        git_track(ctx, new)


# ---- relocating sources into dotfiles_dir ----------------------------------


def relocation_target(ctx: Context, entry: Entry) -> Path | None:
    """Where ``entry``'s source belongs under ``dotfiles_dir``, or ``None`` if it should stay.

    Only plain path sources inside the repository but outside ``dotfiles_dir`` qualify
    (e.g. ``xdg.configFile."niri/config.kdl".source = ./niri/config.kdl;``).
    """
    source = entry.source
    if source is None or entry.linked or entry.inline or not source.exists():
        return None
    if not _inside(source, ctx.config_dir) or _inside(source, ctx.dotfiles_dir):
        return None
    try:
        rel = entry.target.relative_to(Path.home())
    except ValueError:
        return None
    return ctx.dotfiles_dir / rel


def _path_references(ctx: Context, session: Session, source: Path, skip: tuple[Path, int, int]) -> list[str]:
    """Path literals in the repository's .nix files that refer to ``source``, other than ``skip``.

    ``skip`` is ``(file, start, end)``: the span of the binding being rewritten.
    """
    found = []
    for nix in sorted(ctx.config_dir.rglob("*.nix")):
        if ".git" in nix.relative_to(ctx.config_dir).parts:
            continue
        loaded = session.current.get(nix)
        text = loaded.text if loaded else nix.read_text(errors="replace")
        try:
            tokens = tokenize(text)
        except NixParseError:
            continue
        for t in tokens:
            if t.kind != "path" or "${" in t.text or ":" in t.text or t.text.startswith(("<", "~")):
                continue
            if nix == skip[0] and skip[1] <= t.start < skip[2]:
                continue
            ref = (nix.parent / t.text).resolve()
            # A reference to the file itself, to something inside it, or to a directory
            # (below the repository root) that contains it would break if it moved.
            if ref == source or source in ref.parents or (ctx.config_dir in ref.parents and ref in source.parents):
                found.append(f"{nix.relative_to(ctx.config_dir)}: {t.text}")
    return found


def plan_relocate(ctx: Context, session: Session, target: Path) -> Path:
    """Stage moving the source of the dotfile at ``target`` into ``dotfiles_dir``.

    The file's content and the way it is linked stay the same; only its location in
    the repository and the path in the Nix expression change.
    """
    entry = next((e for e in entries(session) if e.target == target), None)
    new = relocation_target(ctx, entry) if entry else None
    if entry is None or new is None or entry.source is None:
        raise ui.EhmError(f"{ui.tilde(target)} is not a dotfile that can be moved into {ui.tilde(ctx.dotfiles_dir)}.")
    old = entry.source
    if new.exists():
        raise ui.EhmError(f"The repository already contains {new}")
    nf = entry.nix_file
    nf.ensure_editable()
    path = (*entry.kind, entry.name, "source")
    binding = nf.find(path)
    if binding is None:
        raise ui.EhmError(f"Could not find where {ui.tilde(target)}'s source is defined.")
    refs = _path_references(ctx, session, old, (nf.path, binding.value_start, binding.value_end))
    if refs:
        raise ui.EhmError(
            f"{old.relative_to(ctx.config_dir)} is also referenced elsewhere, so it was not moved:\n  "
            + "\n  ".join(refs[:10])
        )
    session.save(nf.set_expr(path, relative_nix_path(new, nf.path.parent)))
    session.notes.append(f"Will move {old.relative_to(ctx.config_dir)} → {new.relative_to(ctx.config_dir)}")

    def move() -> None:
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
        _git_record_move(ctx, old, new)
        _prune_empty(old.parent, ctx.config_dir)

    def undo() -> None:
        if new.exists() and not old.exists():
            old.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(new), str(old))
            _git_record_move(ctx, new, old)
            _prune_empty(new.parent, ctx.config_dir)

    session.before_write.append(move)
    session.undo.append(undo)
    return new


def broken_links(generation: Path | None, leaves: set[str]) -> list[Path]:
    """Home Manager links of the current generation whose final target no longer exists."""
    if generation is None:
        return []
    home = Path.home()
    return [home / rel for rel in sorted(leaves) if (home / rel).is_symlink() and not (home / rel).exists()]


def latest_backup_of(path: Path) -> Path | None:
    """The most recent copy of ``path`` (a file under ``$HOME``) in ehm's backups."""
    root = state_dir() / "backups"
    if not root.is_dir():
        return None
    rel = path.relative_to(Path.home())
    for backup in sorted(root.iterdir(), reverse=True):
        if (backup / rel).exists():
            return backup / rel
    return None


def _restore_copy(source: Path, target: Path) -> None:
    """Put content back where a link was removed. Never overwrites existing files."""
    if source.is_dir():
        for root, _, files in os.walk(source):
            rel = Path(root).relative_to(source)
            (target / rel).mkdir(parents=True, exist_ok=True)
            for f in files:
                dst = target / rel / f
                if not (dst.exists() or dst.is_symlink()):
                    shutil.copy2(Path(root) / f, dst, follow_symlinks=False)
    elif not (target.exists() or target.is_symlink()):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def process_pending_after_switch(ctx: Context) -> None:
    """Finish pending adopt/release operations after a successful switch."""
    remaining = []
    for item in load_pending(ctx):
        target = Path(item["target"])
        if item["action"] == "adopt":
            linked = entry_links_exist(target)
            if linked:
                ui.ok(f"Linked: {ui.tilde(target)}")
            else:
                ui.warn(f"Not linked yet: {ui.tilde(target)}")
                remaining.append(item)
        elif item["action"] == "release":
            if is_managed(target):
                ui.warn(f"Still managed by Home Manager: {ui.tilde(target)}")
                remaining.append(item)
                continue
            source = Path(item["source"]) if item.get("source") else None
            if item.get("keep_file") and source and source.exists():
                _restore_copy(source, target)
                ui.ok(f"Restored as a regular file: {ui.tilde(target)}")
            if item.get("discard_source") and source and source.exists():
                discard_source(ctx, source)
    save_pending(ctx, remaining)


def entry_links_exist(target: Path) -> bool:
    """Whether ``target`` (or, for a recursive directory, a file inside it) is linked."""
    if is_managed(target):
        return True
    if target.is_dir():
        for root, _, files in os.walk(target):
            if any(is_managed(Path(root) / f) for f in files):
                return True
    return False


def open_editor(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    subprocess.run([*editor.split(), str(path)])
