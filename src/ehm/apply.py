"""빌드, 변경 미리보기, 충돌 처리, 적용, 업데이트, 롤백."""

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

from . import ui
from .context import NIX, Context, last_error
from .dotfiles import is_managed, new_backup_dir, process_pending_after_switch
from .workspace import git, git_track, is_git_repo, load_pending, untracked_files


def build_generation(ctx: Context) -> Path:
    """sudo 없이 Home Manager 세대만 빌드한다. 설정 오류를 여기서 먼저 잡는다."""
    ui.step("설정 빌드 (아직 아무것도 적용하지 않습니다)")
    r = subprocess.run(
        [*NIX, "build", "--no-link", "--print-out-paths", ctx.activation_attr()],
        stdout=subprocess.PIPE, text=True,
    )
    if r.returncode != 0:
        raise ui.EhmError("빌드에 실패했습니다. 위 오류를 확인해 주세요. 시스템은 바뀌지 않았습니다.")
    return Path(r.stdout.strip().splitlines()[-1])


def current_generation() -> Path | None:
    from .context import xdg_dir

    link = xdg_dir("XDG_STATE_HOME", ".local/state") / "home-manager/gcroots/current-home"
    return link.resolve() if link.exists() else None


def home_file_leaves(generation: Path) -> set[str]:
    """세대가 홈에 만들 파일의 상대 경로들."""
    root = generation / "home-files"
    out: set[str] = set()
    if not root.exists():
        return out
    for cur, dirs, files in os.walk(root):
        base = Path(cur)
        for d in list(dirs):
            if (base / d).is_symlink():  # 폴더 전체를 링크하는 항목
                dirs.remove(d)
                out.add(str((base / d).relative_to(root)))
        for f in files:
            out.add(str((base / f).relative_to(root)))
    return out


def find_conflicts(generation: Path) -> list[Path]:
    """Home Manager 가 덮어쓰기를 거부할, 관리되지 않는 기존 파일들."""
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
        ui.info("이전 세대를 찾지 못해 변경 비교를 건너뜁니다.")
        return
    if old == new:
        ui.ok("Home Manager 쪽 변경은 없습니다.")
        return
    ui.step("바뀌는 내용")
    before, after = home_file_leaves(old), home_file_leaves(new)
    for rel in sorted(after - before):
        print(ui.paint("32", f"  + ~/{rel}"))
    for rel in sorted(before - after):
        print(ui.paint("31", f"  - ~/{rel}"))
    r = subprocess.run([*NIX, "store", "diff-closures", str(old), str(new)], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        text = r.stdout if sys.stdout.isatty() else re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)
        lines = text.strip().splitlines()
        for line in lines[:40]:
            print("  " + line)
        if len(lines) > 40:
            ui.info(ui.dim(f"... 외 {len(lines) - 40}개"))


def move_aside(conflicts: list[Path]) -> list[tuple[Path, Path]]:
    """충돌 파일을 지우지 않고 백업 폴더로 옮긴다."""
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
        ui.info(f"백업: {ui.tilde(backup_root)}")
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
    raise ui.EhmError("home-manager 명령을 찾지 못했습니다.")


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
    """`dot add` 이후 원본이 또 바뀌었으면 알려 준다. 그 내용은 백업에만 남기 때문이다."""
    for item in load_pending(ctx):
        if item["action"] != "adopt":
            continue
        target, source = Path(item["target"]), Path(item["source"])
        for conflict in conflicts:
            if conflict != target and target not in conflict.parents:
                continue
            copy = source / conflict.relative_to(target) if conflict != target else source
            if conflict.is_file() and (not copy.is_file() or not filecmp.cmp(conflict, copy, shallow=False)):
                ui.warn(f"{ui.tilde(conflict)} 이(가) 저장소 복사본과 다릅니다. 최신 내용은 백업 폴더에 남습니다.")


def _resolve_conflicts(ctx: Context, conflicts: list[Path], backup_conflicts: bool) -> list[Path]:
    """옮겨도 되는 충돌 목록을 정한다. 허락이 없으면 중단한다."""
    adopted = _adopted_paths(ctx)
    expected = [c for c in conflicts if any(c == a or a in c.parents for a in adopted)]
    others = [c for c in conflicts if c not in expected]
    if expected:
        ui.info("저장소로 옮긴 파일의 원본은 적용 직전에 백업 폴더로 옮깁니다:")
        for c in expected:
            ui.info(f"  {ui.tilde(c)}")
        _warn_changed_originals(ctx, expected)
    if others:
        ui.warn("Home Manager 가 만들려는 자리에 이미 파일이 있습니다:")
        for c in others:
            ui.info(f"  {ui.tilde(c)}")
        allowed = backup_conflicts or (not ui.ASSUME_YES and ui.confirm("이 파일들을 백업 폴더로 옮기고 계속할까요?"))
        if not allowed:
            raise ui.EhmError(
                "충돌을 해결하지 않아 적용을 중단했습니다.\n"
                "기존 파일을 저장소로 옮기려면 `ehm dot add <파일>`, "
                "백업하고 넘어가려면 `ehm apply --backup-conflicts` 를 쓰세요."
            )
    return conflicts


def check(ctx: Context) -> None:
    """빌드와 변경 미리보기까지만 한다."""
    if not ctx.is_flake:
        raise ui.EhmError("미리보기는 flake 구성에서만 지원합니다. `ehm apply` 를 쓰세요.")
    warn_untracked(ctx)
    new = build_generation(ctx)
    show_changes(current_generation(), new)
    conflicts = find_conflicts(new)
    if conflicts:
        ui.warn(f"적용할 때 처리할 충돌 파일 {len(conflicts)}개:")
        for c in conflicts:
            ui.info(f"  {ui.tilde(c)}")
    ui.ok("빌드 성공. 실제 적용은 `ehm apply` 로 할 수 있습니다.")


def warn_untracked(ctx: Context) -> None:
    if not ctx.is_flake:
        return
    nix_files = [f for f in untracked_files(ctx) if f.endswith(".nix")]
    if nix_files:
        ui.warn("git 이 추적하지 않는 .nix 파일은 flake 에서 보이지 않습니다: " + ", ".join(nix_files))


def apply(ctx: Context, *, backup_conflicts: bool = False, commit: bool = False, push: bool = False) -> None:
    warn_untracked(ctx)
    moved: list[tuple[Path, Path]] = []
    to_move: list[Path] = []
    if ctx.is_flake:
        new = build_generation(ctx)
        show_changes(current_generation(), new)
        to_move = _resolve_conflicts(ctx, find_conflicts(new), backup_conflicts)
    else:
        ui.warn("채널 구성에서는 사전 빌드와 충돌 검사를 건너뜁니다.")

    if ctx.is_nixos:
        ui.info("NixOS 모듈 구성이라 시스템 전체를 함께 다시 빌드합니다 (sudo 필요).")
    cmd = switch_command(ctx)
    ui.info(ui.dim("실행: " + " ".join(cmd)))
    if not ui.confirm("적용할까요?", default=True):
        ui.info("적용하지 않았습니다.")
        return

    moved = move_aside(to_move)
    ui.step("적용")
    try:
        code = subprocess.run(cmd, cwd=ctx.config_dir).returncode
    except KeyboardInterrupt:
        code = 130
    if code != 0:
        restore(moved)
        if moved:
            ui.info("옮겼던 파일을 원래 자리로 되돌렸습니다.")
        raise ui.EhmError(f"적용에 실패했습니다 (종료 코드 {code}).")

    process_pending_after_switch(ctx)
    ui.ok("적용 완료")
    if commit or ctx.settings.get("auto_commit"):
        commit_changes(ctx, push or bool(ctx.settings.get("auto_push")))


def commit_changes(ctx: Context, push: bool) -> None:
    if not is_git_repo(ctx):
        return
    git(ctx, "add", "-u")
    if ctx.dotfiles_dir.exists():
        git_track(ctx, ctx.dotfiles_dir)
    if git(ctx, "diff", "--cached", "--quiet", check=False).returncode == 0:
        ui.info("커밋할 변경이 없습니다.")
        return
    git(ctx, "commit", "-m", f"ehm: apply @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    ui.ok("커밋 완료")
    if push:
        r = git(ctx, "push", check=False)
        if r.returncode == 0:
            ui.ok("푸시 완료")
        else:
            ui.warn("푸시 실패:\n" + last_error(r.stderr))


def update(ctx: Context, inputs: list[str], *, do_apply: bool = True, backup_conflicts: bool = False) -> None:
    ui.step("업데이트 확인")
    if ctx.is_flake:
        code = subprocess.run([*NIX, "flake", "update", *inputs], cwd=ctx.config_dir).returncode
    else:
        code = subprocess.run(["nix-channel", "--update"]).returncode
    if code != 0:
        raise ui.EhmError("업데이트에 실패했습니다.")
    if ctx.is_flake and is_git_repo(ctx):
        if not git(ctx, "diff", "--quiet", "--", "flake.lock", check=False).returncode:
            ui.ok("이미 최신입니다.")
            return
    if do_apply:
        try:
            apply(ctx, backup_conflicts=backup_conflicts)
        except ui.EhmError:
            ui.warn("되돌리려면 설정 폴더에서 `git checkout flake.lock` 을 실행하세요.")
            raise


def generations(ctx: Context) -> None:
    if ctx.is_nixos:
        subprocess.run(["nixos-rebuild", "list-generations"])
    else:
        subprocess.run([*hm_command(ctx), "generations"])


def rollback(ctx: Context) -> None:
    if ctx.is_nixos:
        ui.warn("NixOS 모듈 구성에서는 시스템 전체가 이전 세대로 돌아갑니다.")
        cmd = ["sudo", "nixos-rebuild", "switch", "--rollback"]
    else:
        result = subprocess.run([*hm_command(ctx), "generations"], capture_output=True, text=True)
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            raise ui.EhmError("되돌아갈 이전 세대가 없습니다.")
        previous = lines[1].split()[-1]
        cmd = [f"{previous}/activate"]
    ui.info(ui.dim("실행: " + " ".join(cmd)))
    if not ui.confirm("이전 세대로 되돌릴까요?"):
        return
    if subprocess.run(cmd).returncode != 0:
        raise ui.EhmError("롤백에 실패했습니다.")
    ui.ok("롤백 완료")
    ui.warn(f"{ui.tilde(ctx.config_dir)} 의 설정 파일은 그대로입니다. 다음 적용 때 다시 새 설정이 적용됩니다.")


def collect_garbage(ctx: Context, older_than: str) -> None:
    if ctx.is_nixos:
        cmd = ["sudo", "nix-collect-garbage", "--delete-older-than", older_than]
    else:
        cmd = ["nix-collect-garbage", "--delete-older-than", older_than]
    ui.warn(f"{older_than} 보다 오래된 세대를 지웁니다. 지운 세대로는 롤백할 수 없습니다.")
    ui.info(ui.dim("실행: " + " ".join(cmd)))
    if ui.confirm("계속할까요?"):
        if subprocess.run(cmd).returncode != 0:
            raise ui.EhmError("정리에 실패했습니다.")
        ui.ok("정리 완료")
