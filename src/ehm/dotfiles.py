"""dotfile 을 설정 저장소로 옮겨 Home Manager 가 관리하게 한다."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import ui
from .context import Context, state_dir
from .nixfile import nix_string, relative_nix_path, NixFile
from .workspace import (
    Session, git, git_track, is_git_repo, load_pending, save_pending,
)

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
    (re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY"), "개인 키"),
    (re.compile(rb"\b(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}"), "GitHub 토큰"),
    (re.compile(rb"\bsk-[A-Za-z0-9_\-]{20,}"), "API 키"),
    (re.compile(rb"\bAKIA[0-9A-Z]{16}\b"), "AWS 키"),
    (re.compile(rb"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "Slack 토큰"),
    (
        re.compile(
            rb"(?i)\b[\w\-]*(password|passwd|api[_\-]?key|secret|auth[_\-]?token|access[_\-]?token)[\w\-]*"
            rb"[\"']?\s*[=:]\s*[\"']?[^\s\"'$]{8,}"
        ),
        "비밀번호/키로 보이는 값",
    ),
]


@dataclass
class Entry:
    kind: tuple[str, str]
    name: str
    target: Path
    source_expr: str | None
    source: Path | None  # 저장소 안의 실제 파일
    linked: bool  # mkOutOfStoreSymlink 사용 여부
    inline: bool  # text = ... 로 직접 쓴 항목
    nix_file: NixFile


def is_managed(path: Path) -> bool:
    """Home Manager 가 만든 심볼릭 링크인지."""
    if not path.is_symlink():
        return False
    link = os.readlink(path)
    return link.startswith("/nix/store/") and "home-manager-files" in link


def entries(session: Session) -> list[Entry]:
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


def entry_status(entry: Entry, pending: list[dict]) -> str:
    target = str(entry.target)
    if any(p["target"] == target for p in pending if p["action"] == "adopt"):
        return "적용 대기"
    if is_managed(entry.target):
        return "연결됨"
    if entry.target.is_dir() and not entry.target.is_symlink():
        # recursive 폴더는 안쪽 파일들이 링크된다.
        for root, _, files in os.walk(entry.target):
            if any(is_managed(Path(root) / f) for f in files):
                return "연결됨"
    if entry.target.exists() or entry.target.is_symlink():
        return "충돌 (일반 파일이 있음)"
    return "아직 적용 안 됨"


def find_secrets(path: Path) -> list[tuple[Path, str]]:
    """비밀 정보로 보이는 파일을 찾는다. 값 자체는 돌려주지 않는다."""
    found: list[tuple[Path, str]] = []
    if path.is_dir() and not path.is_symlink():
        files = (Path(r) / f for r, ds, fs in os.walk(path) for f in fs
                 if not (set(Path(r).parts) & IGNORED))
    else:
        files = [path]
    for f in files:
        if set(f.parts) & SECRET_DIRS:
            found.append((f, "민감한 폴더 안의 파일"))
            continue
        if SECRET_NAMES.match(f.name):
            found.append((f, "민감한 파일 이름"))
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
    """전체 크기와, 저장소에 넣을 수 없는 항목(저장소 밖을 가리키는 링크)."""
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
                    problems.append(f"{ui.tilde(p)} (폴더 밖을 가리키는 링크)")
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
    expanded = os.path.expanduser(raw)
    path = Path(os.path.abspath(expanded))
    return path


def plan_add(ctx: Context, session: Session, raw: str, *, copy: bool = False, allow_secrets: bool = False) -> Path:
    """dotfile 추가를 세션에 등록한다. 실제 복사는 저장 확인 뒤에 일어난다."""
    target = resolve_target(raw)
    home = Path.home()
    if not (target.exists() or target.is_symlink()):
        raise ui.EhmError(f"파일이 없습니다: {ui.tilde(target)}")
    try:
        rel = target.relative_to(home)
    except ValueError:
        raise ui.EhmError(f"홈 폴더 안의 파일만 관리할 수 있습니다: {target}") from None
    if str(rel) == ".":
        raise ui.EhmError("홈 폴더 전체는 추가할 수 없습니다.")
    if target == ctx.config_dir or ctx.config_dir in target.parents:
        raise ui.EhmError("설정 저장소 안의 파일은 추가할 수 없습니다.")
    if is_managed(target):
        raise ui.EhmError(
            f"{ui.tilde(target)} 은(는) 이미 Home Manager 가 만든 파일입니다.\n"
            "programs.* 설정에서 생성된 것이라면 `ehm configure` 로 바꾸세요."
        )
    if target.is_symlink():
        raise ui.EhmError(
            f"{ui.tilde(target)} 은(는) 심볼릭 링크입니다 ({os.readlink(target)}).\n"
            "링크가 가리키는 실제 파일을 정리한 뒤 다시 시도해 주세요."
        )
    for e in entries(session):
        if e.target == target or e.target in target.parents or target in e.target.parents:
            raise ui.EhmError(f"이미 관리 중인 경로와 겹칩니다: {ui.tilde(e.target)}")

    secrets = find_secrets(target)
    if secrets and not allow_secrets:
        listing = "\n".join(f"  - {ui.tilde(p)}: {why}" for p, why in secrets[:10])
        raise ui.EhmError(
            "비밀 정보가 들어 있는 것 같아 추가하지 않았습니다.\n"
            f"{listing}\n"
            "dotfile 은 git 저장소와 모두가 읽을 수 있는 /nix/store 에 복사될 수 있습니다.\n"
            "비밀값을 분리한 뒤 다시 시도하세요. 문제가 없다고 확신하면 --allow-secrets 를 쓰세요."
        )
    size, problems = _tree_problems(target)
    if problems:
        raise ui.EhmError("저장소에 넣을 수 없는 항목이 있습니다:\n  " + "\n  ".join(problems[:10]))
    if size > MAX_BYTES:
        raise ui.EhmError(
            f"너무 큽니다 ({size // (1024 * 1024)} MiB). 앱의 캐시/상태 폴더일 수 있으니 "
            "필요한 설정 파일만 골라 추가해 주세요."
        )

    source = ctx.dotfiles_dir / rel
    if source.exists():
        raise ui.EhmError(f"저장소에 같은 경로가 이미 있습니다: {source}")

    rel_str = str(rel)
    if rel_str.startswith(".config/"):
        kind, name = ("xdg", "configFile"), rel_str[len(".config/"):]
    else:
        kind, name = ("home", "file"), rel_str

    # 이미 dotfile 선언이 있는 파일을 우선한다.
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

    how = "수정이 바로 반영되는 링크" if not copy else "빌드할 때 복사되는 읽기 전용 파일"
    session.notes.append(f"복사 예정: {ui.tilde(target)} → {ui.tilde(source)}  ({how})")

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
        raise ui.EhmError(f"'{query}' 에 해당하는 항목이 여러 개입니다: {names}")
    raise ui.EhmError(f"관리 중인 dotfile 중 '{query}' 을(를) 찾지 못했습니다. `ehm dot list` 로 확인하세요.")


def _inside(path: Path | None, parent: Path) -> bool:
    return path is not None and (path == parent or parent in path.parents)


def plan_remove(ctx: Context, session: Session, query: str, *, delete: bool = False) -> None:
    """관리 해제. 기본값은 파일 내용을 원래 자리에 일반 파일로 되돌리는 것."""
    entry = find_entry(session, query)
    if entry.inline and not delete:
        raise ui.EhmError(
            f"{ui.tilde(entry.target)} 은(는) Nix 안에 내용을 직접 쓴 항목이라 되돌릴 원본이 없습니다.\n"
            "파일까지 없애도 된다면 --delete 를 붙여 주세요."
        )
    if entry.source is None and not delete:
        raise ui.EhmError(
            f"{ui.tilde(entry.target)} 의 원본 위치를 알 수 없습니다 ({entry.source_expr}).\n"
            "`ehm edit` 으로 직접 수정하거나, 파일까지 없애도 된다면 --delete 를 붙여 주세요."
        )
    nf = entry.nix_file.unset((*entry.kind, entry.name))
    session.save(nf)

    pending = load_pending(ctx)
    was_pending = next(
        (p for p in pending if p["action"] == "adopt" and p["target"] == str(entry.target)), None
    )
    owned = _inside(entry.source, ctx.dotfiles_dir)

    if was_pending:
        # 아직 적용 전이면 원본은 그대로 있으므로 저장소 복사본만 치운다.
        session.notes.append(f"추가 취소: {ui.tilde(entry.target)} 는 그대로 둡니다.")

        def cancel() -> None:
            items = [p for p in load_pending(ctx) if p is not was_pending and p != was_pending]
            save_pending(ctx, items)
            if entry.source and entry.source.exists():
                discard_source(ctx, entry.source)

        session.after_write.append(cancel)
        return

    if delete:
        session.notes.append(f"다음 적용 때 {ui.tilde(entry.target)} 이(가) 사라집니다.")
    else:
        session.notes.append(f"다음 적용 때 {ui.tilde(entry.target)} 이(가) 일반 파일로 되돌아갑니다.")

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
    """저장소에서 파일을 치운다. 지우지 않고 백업 폴더로 옮긴다."""
    backup = new_backup_dir() / "repo" / source.relative_to(ctx.config_dir)
    backup.parent.mkdir(parents=True, exist_ok=True)
    tracked = is_git_repo(ctx)
    shutil.move(str(source), str(backup))
    if tracked:
        git(ctx, "add", "-A", "--", str(source), check=False)
    parent = source.parent
    while parent != ctx.dotfiles_dir.parent and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _restore_copy(source: Path, target: Path) -> None:
    """적용 후 링크가 사라진 자리에 내용을 되돌린다. 있는 파일은 덮어쓰지 않는다."""
    if source.is_dir():
        for root, dirs, files in os.walk(source):
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
    remaining = []
    for item in load_pending(ctx):
        target = Path(item["target"])
        if item["action"] == "adopt":
            linked = entry_links_exist(target)
            if linked:
                ui.ok(f"연결됨: {ui.tilde(target)}")
            else:
                ui.warn(f"아직 연결되지 않았습니다: {ui.tilde(target)}")
                remaining.append(item)
        elif item["action"] == "release":
            if is_managed(target):
                ui.warn(f"아직 Home Manager 가 관리 중입니다: {ui.tilde(target)}")
                remaining.append(item)
                continue
            source = Path(item["source"]) if item.get("source") else None
            if item.get("keep_file") and source and source.exists():
                _restore_copy(source, target)
                ui.ok(f"일반 파일로 되돌림: {ui.tilde(target)}")
            if item.get("discard_source") and source and source.exists():
                discard_source(ctx, source)
    save_pending(ctx, remaining)


def entry_links_exist(target: Path) -> bool:
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
