"""현재 설정을 분석해 개선안을 찾는다. 찾기만 하고, 고치는 것은 사용자가 고른 뒤에 한다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import dotfiles, ui
from .context import Context
from .nixfile import NixFile, NixParseError, normalize_pkg
from .options import set_option
from .workspace import PACKAGES, Session, git_track, untracked_files


@dataclass
class Suggestion:
    level: str  # 중요 | 추천 | 선택 | 참고
    title: str
    why: str
    fix: Callable[[Session], None] | None = None


# 설정 파일로 보아도 되는 ~/.config 하위 이름들. 브라우저 프로필 같은 앱 상태는 제외한다.
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
    "vscode": "확장과 설정까지 Nix 로 고정할 수 있습니다.",
    "gh": "git 인증 도우미 연동을 설정할 수 있습니다.",
    "ripgrep": "기본 인자를 Nix 로 관리할 수 있습니다.",
    "fastfetch": "표시 항목을 Nix 로 관리할 수 있습니다.",
    "ghostty": "테마와 글꼴을 Nix 로 관리할 수 있습니다.",
}
MAX_SUGGEST_BYTES = 5 * 1024 * 1024


def _migrate(name: str) -> Callable[[Session], None]:
    def fix(session: Session) -> None:
        for nf in session.modules():
            try:
                session.save(nf.remove_list_item(PACKAGES, name))
                break
            except KeyError:
                continue
        set_option(session, ("programs", name, "enable"), "true")

    return fix


def _remove_package(name: str) -> Callable[[Session], None]:
    def fix(session: Session) -> None:
        for nf in session.modules():
            try:
                session.save(nf.remove_list_item(PACKAGES, name))
                return
            except KeyError:
                continue

    return fix


def _enabled_programs(session: Session) -> set[str]:
    out = set()
    for nf in session.modules():
        for b in nf.bindings:
            if len(b.path) == 3 and b.path[0] == "programs" and b.path[2] == "enable":
                if nf.value_text(b) == "true":
                    out.add(b.path[1])
    return out


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
    return {normalize_pkg(i.name) for l in nf.list_info(b) for i in l.items if i.simple}


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
        if not (path.exists() or path.is_symlink()) or path.is_symlink():
            continue
        if any(e.target == path or path in e.target.parents or e.target in path.parents for e in managed):
            continue
        if ctx.config_dir == path or path in ctx.config_dir.parents:
            continue
        if path.is_dir():
            if dotfiles.entry_links_exist(path):  # 일부는 이미 Home Manager 가 만든 폴더
                continue
            if _dir_size(path) > MAX_SUGGEST_BYTES:
                continue
            if not any(f for _, _, fs in os.walk(path) for f in fs):
                continue
        secrets = dotfiles.find_secrets(path)
        if secrets:
            # 폴더라면 비밀이 없는 파일만 따로 제안한다.
            secret_paths = {p for p, _ in secrets}
            clean = [p for p in sorted(path.rglob("*")) if p.is_file() and p not in secret_paths] if path.is_dir() else []
            if clean and len(clean) <= 3:
                for p in clean:
                    out.append(Suggestion(
                        "추천", f"{ui.tilde(p)} 를 저장소로 옮겨 관리",
                        f"같은 폴더의 {', '.join(s.name for s in secret_paths)} 은(는) 비밀 정보로 보여 제외했습니다.",
                        _adopt(ctx, p),
                    ))
            else:
                skipped.append(ui.tilde(path))
            continue
        out.append(Suggestion(
            "추천", f"{ui.tilde(path)} 를 저장소로 옮겨 관리",
            "다른 컴퓨터에서도 같은 설정을 쓸 수 있고 변경 이력이 git 에 남습니다. 수정은 바로 반영됩니다.",
            _adopt(ctx, path),
        ))
    if skipped:
        out.append(Suggestion(
            "참고", f"비밀 정보가 의심돼 자동 제안에서 뺀 파일: {', '.join(skipped)}",
            "토큰이나 비밀번호를 다른 파일로 분리하면 안전하게 저장소에 넣을 수 있습니다.",
        ))
    return out


def analyze(ctx: Context, session: Session) -> list[Suggestion]:
    out: list[Suggestion] = []
    packages = session.package_names()
    enabled = _enabled_programs(session)

    # 1. flake 에서 보이지 않는 파일: 빌드가 깨지는 흔한 원인
    if ctx.is_flake:
        untracked = untracked_files(ctx)
        safe = [f for f in untracked if f.endswith(".nix")]
        if safe:
            def fix_track(s: Session, files=tuple(safe)) -> None:
                s.notes.append("git add: " + ", ".join(files))
                s.after_write.append(lambda: git_track(ctx, *[ctx.config_dir / f for f in files]))

            out.append(Suggestion(
                "중요", f"git 에 추가되지 않은 .nix 파일: {', '.join(safe)}",
                "flake 는 git 이 추적하는 파일만 읽습니다. 추가하지 않으면 이 파일을 쓰는 빌드가 실패합니다.",
                fix_track,
            ))
        others = [f for f in untracked if not f.endswith(".nix")]
        if others:
            shown = ", ".join(others[:5]) + (" 등" if len(others) > 5 else "")
            out.append(Suggestion(
                "참고", f"git 이 추적하지 않는 파일: {shown}",
                "설정에서 쓰는 파일이면 직접 `git add` 해 주세요. 비밀 정보가 없는지 먼저 확인하세요.",
            ))

    # 2. programs.X 로 켜져 있는데 패키지 목록에도 있는 경우
    for name in sorted(packages & enabled):
        out.append(Suggestion(
            "추천", f"home.packages 의 {name} 제거 (programs.{name} 와 중복)",
            f"programs.{name}.enable 이 이미 패키지를 설치합니다.",
            _remove_package(name),
        ))

    # 3. 전용 모듈이 있는 패키지
    modules: list[str] = []
    if ctx.is_flake:
        try:
            modules = ctx.program_modules()
        except ui.EhmError as e:
            ui.warn(f"프로그램 모듈 목록을 읽지 못해 일부 분석을 건너뜁니다: {e}")
    for name in sorted((packages & set(modules)) - enabled):
        note = PROGRAM_NOTES.get(name, "설정까지 Nix 로 관리할 수 있습니다.")
        out.append(Suggestion(
            "선택", f"{name} 을(를) programs.{name} 모듈로 전환",
            f"{note} 옵션을 쓸 계획이 없다면 그대로 둬도 됩니다.",
            _migrate(name),
        ))

    # 4. 관리되지 않는 dotfile
    out += _dotfile_candidates(ctx, session, packages)

    # 5. 참고 사항
    system = _system_packages(ctx) if ctx.is_nixos else set()
    duplicated = sorted(system & (packages | enabled))
    if duplicated:
        out.append(Suggestion(
            "참고", f"시스템과 사용자 양쪽에 있는 패키지: {', '.join(duplicated)}",
            "대부분 문제없지만 한쪽만 남기면 어느 버전을 쓰는지 분명해집니다. "
            "시스템 패키지는 configuration.nix 에서 직접 정리해 주세요.",
        ))
    if ctx.mode == "nixos-flake":
        flake = (ctx.config_dir / "flake.nix").read_text()
        if "backupFileExtension" not in flake:
            out.append(Suggestion(
                "참고", "home-manager.backupFileExtension 이 없습니다",
                "ehm 은 충돌 파일을 알아서 백업하지만, nixos-rebuild 를 직접 실행할 때는 기존 파일이 있으면 "
                'Home Manager 활성화가 실패합니다. flake.nix 의 home-manager 설정에 '
                'home-manager.backupFileExtension = "backup"; 을 넣으면 예방됩니다.',
            ))
    return out


def order_key(s: Suggestion) -> int:
    return {"중요": 0, "추천": 1, "선택": 2, "참고": 3}.get(s.level, 4)


# ---- 추천 프로그램 묶음 --------------------------------------------------
BUNDLES: dict[str, dict[str, str]] = {
    "터미널 도구": {
        "bat": "문법 강조가 되는 cat",
        "eza": "보기 좋은 ls",
        "fzf": "무엇이든 빠르게 찾는 퍼지 검색",
        "zoxide": "자주 가는 폴더로 빠른 이동",
        "btop": "시스템 모니터",
        "yazi": "터미널 파일 관리자",
    },
    "셸 환경": {
        "zsh": "Home Manager 로 관리하는 zsh (~/.zshrc 를 대체)",
        "starship": "셸 프롬프트",
        "atuin": "셸 기록 검색",
    },
    "개발": {
        "direnv": "폴더별 개발 환경 자동 적용",
        "lazygit": "터미널 git UI",
        "gh": "GitHub CLI",
        "jq": "JSON 처리",
    },
}
