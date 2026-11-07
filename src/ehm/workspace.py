"""설정 파일 묶음과 변경 세션. 모든 수정은 diff 확인을 거쳐 한꺼번에 저장된다."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import ui
from .context import Context, last_error, state_dir
from .nixfile import ModuleShapeError, NixFile, NixParseError, normalize_pkg

PACKAGES = ("home", "packages")


class Session:
    """여러 .nix 파일의 변경을 모아 두었다가 확인 후 저장한다."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.originals: dict[Path, str] = {}
        self.current: dict[Path, NixFile] = {}
        self.notes: list[str] = []
        self.before_write: list[Callable[[], None]] = []
        self.after_write: list[Callable[[], None]] = []
        self.undo: list[Callable[[], None]] = []  # 저장 후 검사가 실패했을 때 되돌리는 작업

    def load(self, path: Path) -> NixFile:
        if path not in self.current:
            text = path.read_text()
            self.originals[path] = text
            try:
                self.current[path] = NixFile(text, path)
            except NixParseError as e:
                raise ui.EhmError(f"{path} 를 읽지 못했습니다: {e}") from e
        return self.current[path]

    def save(self, nf: NixFile) -> None:
        self.current[nf.path] = nf

    # ---- 모듈 파일 탐색 ----------------------------------------------
    def module_files(self) -> list[Path]:
        """home 파일과, 그 파일이 상대 경로로 불러오는 파일들."""
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

    def packages(self) -> list[tuple[NixFile, str, bool]]:
        """(파일, 항목 이름, 단순 항목 여부) 목록."""
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

    def file_with_packages(self) -> NixFile:
        for nf in self.modules():
            if nf.find(PACKAGES) is not None:
                return nf
        return self.load(self.ctx.home_file)

    def file_with_path(self, path: tuple[str, ...]) -> NixFile | None:
        """해당 경로 또는 상위 속성 집합이 선언된 파일을 찾는다."""
        for n in range(len(path), 0, -1):
            for nf in self.modules():
                if nf.find(path[:n]) is not None:
                    return nf
        return None

    # ---- 저장 --------------------------------------------------------
    def changed_files(self) -> list[Path]:
        return [p for p, nf in self.current.items() if nf.text != self.originals[p]]

    def has_changes(self) -> bool:
        return bool(self.changed_files() or self.notes or self.before_write or self.after_write)

    def show(self) -> None:
        for path in self.changed_files():
            ui.show_diff(path, self.originals[path], self.current[path].text)
        for note in self.notes:
            ui.info(note)

    def _validate(self) -> None:
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
                        f"변경 결과가 올바른 Nix 문법이 아니라 저장하지 않았습니다 ({path}):\n"
                        + last_error(r.stderr)
                        + "\n이 도구의 버그일 수 있습니다. `ehm edit` 으로 직접 고쳐 주세요."
                    )
            finally:
                os.unlink(tmp)

    def commit(self, question: str = "이대로 저장할까요?") -> bool:
        """diff 를 보여주고 확인을 받은 뒤 저장한다. 저장했으면 True."""
        if not self.has_changes():
            ui.info("바꿀 내용이 없습니다.")
            return False
        self.show()
        self._validate()
        if not ui.confirm(question, default=True):
            ui.info("저장하지 않았습니다.")
            return False
        for fn in self.before_write:
            fn()
        for path in self.changed_files():
            write_atomic(path, self.current[path].text)
        for fn in self.after_write:
            fn()
        for path in list(self.current):
            self.originals[path] = self.current[path].text
        self.notes.clear()
        self.before_write.clear()
        self.after_write.clear()
        return True


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


# ---- 대기 중인 작업(pending) ----------------------------------------
# dotfile 을 넣거나 뺄 때 실제 파일 이동은 다음 적용 때 이루어지므로 기록해 둔다.


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
    r = subprocess.run(["git", "-C", str(ctx.config_dir), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise ui.EhmError(f"git {' '.join(args)} 실패:\n" + last_error(r.stderr))
    return r


def git_track(ctx: Context, *paths: Path) -> None:
    """flake 는 git 이 추적하는 파일만 볼 수 있으므로 새 파일을 스테이징한다."""
    if is_git_repo(ctx):
        git(ctx, "add", "-A", "--", *[str(p) for p in paths])


def untracked_files(ctx: Context) -> list[str]:
    if not is_git_repo(ctx):
        return []
    r = git(ctx, "ls-files", "--others", "--exclude-standard")
    return [l for l in r.stdout.splitlines() if l]


def guard_shape(fn):
    """파일 모양 때문에 자동 수정이 불가능할 때 친절한 오류로 바꾼다."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ModuleShapeError as e:
            raise ui.EhmError(f"{e}\n`ehm edit` 으로 직접 수정해 주세요.") from e

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper
