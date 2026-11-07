"""명령줄 진입점과 대화형 메뉴."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, apply as applier, dotfiles, options, suggest, ui
from .context import MODE_LABELS, NIX, Context, detect, last_error, settings_path, state_dir
from .nixfile import ModuleShapeError, format_path, split_option_path, to_nix
from .workspace import PACKAGES, Session, git, is_git_repo, load_pending, untracked_files, write_atomic

ATTR_RE = re.compile(r"^[A-Za-z_][\w'\-]*(\.[A-Za-z_][\w'\-]*)*$")


# ---- 공통 흐름 ---------------------------------------------------------
def verify_after_save(ctx: Context, session: Session, snapshot: dict[Path, str]) -> bool:
    """저장 후 바로 평가해 보고, 깨졌으면 되돌릴 기회를 준다."""
    if not ctx.is_flake:
        return True
    ui.info("변경한 설정을 검사하는 중...")
    r = subprocess.run(
        [*NIX, "eval", "--raw", f"{ctx.activation_attr()}.drvPath"], capture_output=True, text=True
    )
    if r.returncode == 0:
        ui.ok("설정 검사 통과")
        return True
    ui.error("설정 평가에 실패했습니다:\n" + last_error(r.stderr))
    if ui.confirm("방금 바꾼 파일을 되돌릴까요?", default=True):
        for path, text in snapshot.items():
            write_atomic(path, text)
        for undo in session.undo:
            undo()
        ui.ok("되돌렸습니다.")
    return False


def finish(ctx: Context, session: Session, args=None, question: str = "이대로 저장할까요?") -> bool:
    """diff 확인 → 저장 → 검사 → (선택) 적용까지의 공통 흐름."""
    snapshot = {p: session.originals[p] for p in session.changed_files()}
    if not session.commit(question):
        return False
    ui.ok("저장했습니다.")
    if not verify_after_save(ctx, session, snapshot):
        raise ui.EhmError("설정 검사를 통과하지 못했습니다.")
    want_apply = bool(args and getattr(args, "apply", False))
    if want_apply:
        applier.apply(ctx, backup_conflicts=getattr(args, "backup_conflicts", False))
    elif ui.ASSUME_YES:
        ui.info("실제로 반영하려면 `ehm apply` 를 실행하세요.")
    elif ui.confirm("지금 적용할까요?"):
        applier.apply(ctx)
    else:
        ui.info("나중에 `ehm apply` 로 적용할 수 있습니다.")
    return True


# ---- 패키지 -------------------------------------------------------------
def cmd_add(ctx: Context, args) -> None:
    session = Session(ctx)
    names = session.package_names()
    for raw in args.packages:
        name = raw.removeprefix("nixpkgs#").removeprefix("pkgs.")
        if not ATTR_RE.match(name):
            raise ui.EhmError(f"패키지 이름이 올바르지 않습니다: {raw}")
        if name in names:
            ui.info(f"{name} 은(는) 이미 설치 목록에 있습니다.")
            continue
        if not args.no_check:
            found = ctx.package_info(name)
            if found is None:
                ui.warn(f"nixpkgs 에 '{name}' 이(가) 없습니다.")
                if not ui.ASSUME_YES and ui.confirm("비슷한 패키지를 검색할까요?", default=True):
                    run_search(ctx, [name], limit=10)
                raise ui.EhmError(f"'{name}' 을(를) 추가하지 않았습니다.")
            desc = f" — {found['description']}" if found.get("description") else ""
            ui.ok(f"{found.get('name') or name}{desc}")
        nf = session.file_with_packages()
        session.save(nf.add_list_item(PACKAGES, name))
        names.add(name)
        tip_program(ctx, name)
    if session.has_changes():
        finish(ctx, session, args)


def tip_program(ctx: Context, name: str) -> None:
    if not ctx.is_flake:
        return
    try:
        if name in ctx.program_modules():
            ui.info(ui.dim(f"팁: 설정까지 관리하려면 `ehm enable {name}` 을 쓸 수 있습니다."))
    except ui.EhmError:
        pass


def cmd_remove(ctx: Context, args) -> None:
    session = Session(ctx)
    for raw in args.packages:
        name = raw.removeprefix("pkgs.")
        for nf in session.modules():
            try:
                session.save(nf.remove_list_item(PACKAGES, name))
                break
            except KeyError:
                continue
        else:
            complex_items = [n for _, n, simple in session.packages() if not simple and name in n]
            hint = f"\n복잡한 항목에 들어 있습니다: {complex_items[0]}\n`ehm edit` 으로 직접 수정하세요." if complex_items else ""
            enabled = next((nf for nf in session.modules() if nf.has_path(("programs", name, "enable"))), None)
            if enabled:
                hint = f"\nprograms.{name} 모듈로 켜져 있습니다. `ehm disable {name}` 을 쓰세요."
            raise ui.EhmError(f"home.packages 에서 '{name}' 을(를) 찾지 못했습니다.{hint}")
    finish(ctx, session, args)


def cmd_list(ctx: Context, args) -> None:
    if args.all:
        ctx.require_flake()
        names = ctx.nix_eval_json(
            f"{ctx.hm_config_attr()}.home.packages",
            'map (p: p.name or (p.pname or "?"))',
        )
        for name in sorted(set(names)):
            print(name)
        ui.info(ui.dim(f"총 {len(set(names))}개 (programs.* 모듈이 설치한 것 포함)"))
        return
    session = Session(ctx)
    items = session.packages()
    for nf, name, simple in items:
        print(name if simple else ui.dim(name))
    enabled = suggest._enabled_programs(session)
    if enabled:
        print(ui.bold("\nprograms.* 로 켠 프로그램"))
        for name in sorted(enabled):
            print(name)
    ui.info(ui.dim(f"\n패키지 {len(items)}개, 모듈 {len(enabled)}개"))


def run_search(ctx: Context, terms: list[str], limit: int = 25) -> None:
    ui.info("검색 중... (처음에는 1분 정도 걸릴 수 있습니다)")
    cmd = [*NIX, "search", *ctx.inputs_args(), "nixpkgs", *terms, "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ui.EhmError("검색에 실패했습니다:\n" + last_error(r.stderr))
    data = json.loads(r.stdout or "{}")
    installed = Session(ctx).package_names()
    rows = []
    for key, value in data.items():
        attr = key.split(".", 2)[2] if key.count(".") >= 2 else key
        rows.append((attr, value.get("version", ""), value.get("description", "")))
    needle = terms[0].lower()
    rows.sort(key=lambda r: (r[0].lower() != needle, not r[0].lower().startswith(needle), len(r[0]), r[0]))
    if not rows:
        ui.info("결과가 없습니다.")
        return
    for attr, version, desc in rows[:limit]:
        mark = ui.paint("32", " [설치됨]") if attr in installed else ""
        print(f"{ui.bold(attr)} {ui.dim(version)}{mark}")
        if desc:
            print(f"    {desc}")
    if len(rows) > limit:
        ui.info(ui.dim(f"... 외 {len(rows) - limit}개. 검색어를 더 구체적으로 적어 보세요."))
    print(ui.dim("\n설치: ehm add <이름>"))


def cmd_search(ctx: Context, args) -> None:
    run_search(ctx, args.terms, args.limit)


# ---- dotfile ------------------------------------------------------------
def cmd_dot_add(ctx: Context, args) -> None:
    session = Session(ctx)
    for path in args.paths:
        dotfiles.plan_add(ctx, session, path, copy=args.copy, allow_secrets=args.allow_secrets)
    finish(ctx, session, args)


def cmd_dot_rm(ctx: Context, args) -> None:
    session = Session(ctx)
    for query in args.targets:
        dotfiles.plan_remove(ctx, session, query, delete=args.delete)
    finish(ctx, session, args)


def cmd_dot_list(ctx: Context, args) -> None:
    session = Session(ctx)
    pending = load_pending(ctx)
    items = dotfiles.entries(session)
    if not items:
        ui.info("관리 중인 dotfile 이 없습니다. `ehm dot add <파일>` 로 추가하세요.")
    for e in items:
        how = "링크" if e.linked else ("내용 직접 작성" if e.inline else "복사")
        status = dotfiles.entry_status(e, pending)
        color = {"연결됨": "32", "적용 대기": "33"}.get(status, "31")
        src = ui.tilde(e.source) if e.source else (e.source_expr or "")
        print(f"{ui.bold(ui.tilde(e.target))}  {ui.paint(color, status)}")
        print(ui.dim(f"    {how}: {src}"))
    for p in pending:
        if p["action"] == "release":
            print(f"{ui.tilde(p['target'])}  {ui.paint('33', '관리 해제 대기')}")
    if args.all:
        gen = applier.current_generation()
        if gen:
            print(ui.bold("\n현재 세대가 만든 모든 파일 (programs.* 생성 포함)"))
            for rel in sorted(applier.home_file_leaves(gen)):
                print(f"~/{rel}")


def cmd_dot_edit(ctx: Context, args) -> None:
    session = Session(ctx)
    entry = dotfiles.find_entry(session, args.target)
    if entry.source is None:
        ui.info(f"{ui.tilde(entry.target)} 은(는) Nix 안에 정의되어 있어 설정 파일을 엽니다.")
        dotfiles.open_editor(entry.nix_file.path)
        return
    if entry.source.is_dir():
        raise ui.EhmError(f"폴더입니다. 안의 파일을 직접 여세요: {entry.source}")
    dotfiles.open_editor(entry.source)
    if entry.linked:
        ui.ok("링크로 연결된 파일이라 바로 반영됩니다.")
    else:
        ui.info("복사 방식 항목이라 `ehm apply` 후 반영됩니다.")


# ---- 옵션 ---------------------------------------------------------------
def cmd_programs(ctx: Context, args) -> None:
    ctx.require_flake()
    modules = ctx.program_modules()
    session = Session(ctx)
    enabled = suggest._enabled_programs(session)
    query = (args.query or "").lower()
    shown = [m for m in modules if query in m.lower()]
    for name in shown:
        mark = ui.paint("32", "  [켜짐]") if name in enabled else ""
        print(f"{name}{mark}")
    ui.info(ui.dim(f"{len(shown)}개. 켜기: ehm enable <이름>, 옵션 고르기: ehm configure <이름>"))


def _check_program(ctx: Context, path: tuple[str, ...]) -> None:
    if ctx.is_flake and len(path) == 2 and path[0] == "programs":
        modules = ctx.program_modules()
        if path[1] not in modules:
            close = [m for m in modules if path[1].lower() in m.lower()][:8]
            hint = f"\n비슷한 이름: {', '.join(close)}" if close else ""
            raise ui.EhmError(
                f"programs.{path[1]} 모듈이 없습니다.{hint}\n패키지만 설치하려면 `ehm add {path[1]}` 을 쓰세요."
            )


def cmd_enable(ctx: Context, args) -> None:
    session = Session(ctx)
    for name in args.names:
        path = options.normalize_program(name)
        _check_program(ctx, path)
        options.set_option(session, (*path, "enable"), "true")
        if len(path) == 2 and path[0] == "programs":
            for nf in session.modules():
                try:
                    session.save(nf.remove_list_item(PACKAGES, path[1]))
                    session.notes.append(f"home.packages 의 {path[1]} 은(는) 모듈이 설치하므로 뺐습니다.")
                    break
                except KeyError:
                    continue
    finish(ctx, session, args)


def cmd_disable(ctx: Context, args) -> None:
    session = Session(ctx)
    for name in args.names:
        path = options.normalize_program(name)
        if not options.unset_option(session, (*path, "enable")):
            raise ui.EhmError(f"{format_path(path)}.enable 설정을 찾지 못했습니다.")
    finish(ctx, session, args)


def cmd_set(ctx: Context, args) -> None:
    path = split_option_path(args.option)
    session = Session(ctx)
    if args.nix:
        expr = args.value
    elif args.str:
        expr = to_nix(args.value)
    else:
        expr = to_nix(options.parse_auto(args.value))
    options.set_option(session, path, expr)
    finish(ctx, session, args)


def cmd_unset(ctx: Context, args) -> None:
    path = split_option_path(args.option)
    session = Session(ctx)
    if not options.unset_option(session, path):
        raise ui.EhmError(f"{format_path(path)} 설정을 찾지 못했습니다.")
    finish(ctx, session, args)


def cmd_get(ctx: Context, args) -> None:
    path = split_option_path(args.option)
    session = Session(ctx)
    for nf in session.modules():
        b = nf.find(path)
        if b:
            print(ui.dim(f"# {ui.tilde(nf.path)}"))
            print(nf.value_text(b))
    if ctx.is_flake:
        attr = f"{ctx.hm_config_attr()}.{format_path(path)}"
        r = subprocess.run(
            [*NIX, "eval", "--json", attr], capture_output=True, text=True
        )
        if r.returncode == 0:
            print(ui.dim("# 최종 값"))
            print(r.stdout.strip())
        else:
            raise ui.EhmError("값을 읽지 못했습니다:\n" + last_error(r.stderr, 4))


def cmd_configure(ctx: Context, args) -> None:
    name = args.program
    if not name:
        ctx.require_flake()
        session = Session(ctx)
        enabled = sorted(suggest._enabled_programs(session))
        if enabled:
            ui.info("켜져 있는 프로그램: " + ", ".join(enabled))
        name = ui.ask("설정할 프로그램 이름 (예: git)")
        if not name:
            return
    session = options.configure(ctx, name)
    if session and session.has_changes():
        finish(ctx, session, args)


# ---- 제안 / 묶음 ---------------------------------------------------------
def cmd_suggest(ctx: Context, args) -> None:
    session = Session(ctx)
    ui.step("현재 설정 분석")
    items = sorted(suggest.analyze(ctx, session), key=suggest.order_key)
    fixable = [s for s in items if s.fix]
    notes = [s for s in items if not s.fix]
    if not items:
        ui.ok("지금은 제안할 내용이 없습니다. 설정이 잘 정리되어 있어요!")
        return
    colors = {"중요": "31", "추천": "36", "선택": "35", "참고": "2"}
    for i, s in enumerate(fixable, 1):
        print(f"  {i:>2}) {ui.paint(colors[s.level], '[' + s.level + ']')} {s.title}")
        print(ui.dim(f"      {s.why}"))
    for s in notes:
        print(f"   · {ui.paint(colors[s.level], '[' + s.level + ']')} {s.title}")
        print(ui.dim(f"      {s.why}"))
    if not fixable:
        return
    if args.report:
        ui.info(ui.dim("고르려면 `ehm suggest` 를 옵션 없이 실행하세요."))
        return
    print()
    chosen = list(range(len(fixable))) if ui.ASSUME_YES and args.all else ui.choose_many("적용할 제안을 고르세요", len(fixable))
    if not chosen:
        ui.info("아무것도 바꾸지 않았습니다.")
        return
    session = Session(ctx)
    for idx in chosen:
        try:
            fixable[idx].fix(session)
        except ui.EhmError as e:
            ui.warn(f"'{fixable[idx].title}' 건너뜀: {e}")
    finish(ctx, session, args)


def cmd_setup(ctx: Context, args) -> None:
    ctx.require_flake()
    modules = set(ctx.program_modules())
    session = Session(ctx)
    enabled = suggest._enabled_programs(session)
    flat: list[tuple[str, str]] = []
    for group, programs in suggest.BUNDLES.items():
        print(ui.bold(group))
        for name, desc in programs.items():
            if name not in modules:
                continue
            if name in enabled:
                print(ui.dim(f"       {name}: 이미 켜짐"))
                continue
            flat.append((name, desc))
            print(f"  {len(flat):>3}) {name}: {desc}")
    if not flat:
        ui.ok("추천 프로그램이 모두 켜져 있습니다.")
        return
    picked = ui.choose_many("켤 프로그램을 고르세요", len(flat))
    if not picked:
        return
    fake = argparse.Namespace(names=[flat[i][0] for i in picked], apply=getattr(args, "apply", False))
    if "zsh" in fake.names and (Path.home() / ".zshrc").exists():
        ui.warn("programs.zsh 는 ~/.zshrc 를 대체합니다. 적용할 때 기존 파일은 백업 폴더로 옮겨집니다.")
    cmd_enable(ctx, fake)


# ---- 기타 ---------------------------------------------------------------
def cmd_apply(ctx: Context, args) -> None:
    applier.apply(ctx, backup_conflicts=args.backup_conflicts, commit=args.commit, push=args.push)


def cmd_check(ctx: Context, args) -> None:
    applier.check(ctx)


def cmd_update(ctx: Context, args) -> None:
    applier.update(ctx, args.inputs, do_apply=not args.no_apply, backup_conflicts=args.backup_conflicts)


def cmd_generations(ctx: Context, args) -> None:
    applier.generations(ctx)


def cmd_rollback(ctx: Context, args) -> None:
    applier.rollback(ctx)


def cmd_gc(ctx: Context, args) -> None:
    applier.collect_garbage(ctx, args.older_than)


def cmd_edit(ctx: Context, args) -> None:
    before = ctx.home_file.read_text()
    dotfiles.open_editor(ctx.home_file)
    if ctx.home_file.read_text() != before:
        session = Session(ctx)
        verify_after_save_edit(ctx, session)


def verify_after_save_edit(ctx: Context, session: Session) -> None:
    if verify_after_save(ctx, session, {}) and not ui.ASSUME_YES and ui.confirm("지금 적용할까요?"):
        applier.apply(ctx)


def cmd_status(ctx: Context, args) -> None:
    session = Session(ctx)
    print(ui.bold("구성"))
    print(f"  방식      {MODE_LABELS[ctx.mode]}")
    print(f"  폴더      {ui.tilde(ctx.config_dir)}")
    print(f"  설정 파일 {ui.tilde(ctx.home_file)}")
    print(f"  사용자    {ctx.user}" + (f"   대상: {ctx.host}" if ctx.host else ""))
    files = session.module_files()
    if len(files) > 1:
        print(f"  모듈      {', '.join(ui.tilde(f) for f in files[1:])}")
    print(ui.bold("관리 중"))
    print(f"  패키지    {len(session.packages())}개")
    print(f"  모듈      {len(suggest._enabled_programs(session))}개")
    print(f"  dotfile   {len(dotfiles.entries(session))}개")
    pending = load_pending(ctx)
    if pending:
        print(ui.bold("적용 대기"))
        for p in pending:
            label = "추가" if p["action"] == "adopt" else "해제"
            print(f"  {label}: {ui.tilde(p['target'])}")
    if is_git_repo(ctx):
        r = git(ctx, "status", "--short")
        changed = [l for l in r.stdout.splitlines() if l]
        print(ui.bold("git"))
        print(f"  미커밋 변경 {len(changed)}개" + (f", 미추적 {len(untracked_files(ctx))}개" if untracked_files(ctx) else ""))
    else:
        print(ui.dim("git 저장소가 아닙니다. 변경 이력을 남기려면 `git init` 을 추천합니다."))
    for nf in session.modules():
        if nf.shape_error:
            ui.warn(f"{ui.tilde(nf.path)}: 자동 수정 불가 — {nf.shape_error}")


def cmd_config(ctx_override: str | None) -> None:
    path = settings_path()
    print(f"ehm 설정 파일: {path}" + ("" if path.exists() else " (아직 없음, 기본값 사용)"))
    print("쓸 수 있는 항목:")
    print('  config_dir = "/etc/nixos"        # 설정 폴더')
    print('  home_file = "home.nix"           # 폴더 기준 상대 경로')
    print('  host = "nixos"                   # flake 안의 대상 이름')
    print('  dotfiles_dir = "dotfiles"        # dotfile 저장 폴더')
    print('  extra_rebuild_args = ["--show-trace"]')
    print("  auto_commit = false              # 적용 후 자동 커밋")
    print("  auto_push = false                # 자동 커밋 후 푸시")
    print(f"백업·상태 폴더: {state_dir()}")


FLAKE_TEMPLATE = """{
  description = "@USER@ 의 Home Manager 설정";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-manager, ... }: {
    homeConfigurations."@USER@" = home-manager.lib.homeManagerConfiguration {
      pkgs = nixpkgs.legacyPackages.@SYSTEM@;
      modules = [ ./home.nix ];
    };
  };
}
"""

HOME_TEMPLATE = """{ config, pkgs, ... }:

{
  home.username = "@USER@";
  home.homeDirectory = "@HOME@";
  home.stateVersion = "@STATE@"; # 처음 설치한 버전. 올리지 마세요.

  home.packages = with pkgs; [
  ];

  programs.home-manager.enable = true;
}
"""


def cmd_init(args) -> None:
    import getpass
    from .context import xdg_dir

    directory = Path(args.dir).expanduser().resolve() if args.dir else xdg_dir("XDG_CONFIG_HOME", ".config") / "home-manager"
    if (directory / "flake.nix").exists() or (directory / "home.nix").exists():
        raise ui.EhmError(f"이미 설정이 있습니다: {directory}")
    if shutil.which("nix") is None:
        raise ui.EhmError("nix 를 찾지 못했습니다. 먼저 Nix 를 설치해 주세요.")
    system = subprocess.run(
        [*NIX, "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"],
        capture_output=True, text=True,
    ).stdout.strip()
    state = subprocess.run(
        [*NIX, "eval", "--raw", "nixpkgs#lib.trivial.release"], capture_output=True, text=True
    ).stdout.strip()
    if not system or not state:
        raise ui.EhmError("시스템 정보나 nixpkgs 버전을 읽지 못했습니다. 네트워크를 확인해 주세요.")
    user = getpass.getuser()
    values = {"@USER@": user, "@HOME@": str(Path.home()), "@SYSTEM@": system, "@STATE@": state}

    def fill(template: str) -> str:
        for key, value in values.items():
            template = template.replace(key, value)
        return template

    ui.info(f"{directory} 에 flake.nix 와 home.nix 를 만듭니다 (사용자 {user}, {system}).")
    if not ui.confirm("계속할까요?", default=True):
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "flake.nix").write_text(fill(FLAKE_TEMPLATE))
    (directory / "home.nix").write_text(fill(HOME_TEMPLATE))
    if not (directory / ".git").exists():
        subprocess.run(["git", "-C", str(directory), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(directory), "add", "flake.nix", "home.nix"], check=True)
    ui.ok("만들었습니다. 다음으로 `ehm apply` 를 실행하세요.")


# ---- 대화형 메뉴 ---------------------------------------------------------
def _ns(**kw) -> argparse.Namespace:
    kw.setdefault("apply", False)
    kw.setdefault("backup_conflicts", False)
    return argparse.Namespace(**kw)


def menu(ctx: Context) -> None:
    def install() -> None:
        name = ui.ask("설치할 패키지 이름 (여러 개는 공백으로)")
        if name:
            cmd_add(ctx, _ns(packages=name.split(), no_check=False))

    def search() -> None:
        q = ui.ask("검색어")
        if q:
            run_search(ctx, q.split())

    def remove() -> None:
        session = Session(ctx)
        items = sorted(session.package_names())
        if not items:
            ui.info("패키지가 없습니다.")
            return
        for i, n in enumerate(items, 1):
            print(f"  {i:>3}) {n}")
        picked = ui.choose_many("제거할 패키지를 고르세요", len(items))
        if picked:
            cmd_remove(ctx, _ns(packages=[items[i] for i in picked]))

    def dot_add() -> None:
        path = ui.ask("추가할 파일이나 폴더 (예: ~/.config/ghostty)")
        if path:
            cmd_dot_add(ctx, _ns(paths=[path], copy=False, allow_secrets=False))

    def dot_rm() -> None:
        session = Session(ctx)
        items = dotfiles.entries(session)
        if not items:
            ui.info("관리 중인 dotfile 이 없습니다.")
            return
        idx = ui.choose("관리에서 뺄 항목", [ui.tilde(e.target) for e in items])
        if idx is not None:
            cmd_dot_rm(ctx, _ns(targets=[str(items[idx].target)], delete=False))

    actions = [
        ("설정 점검 + 개선 제안", lambda: cmd_suggest(ctx, _ns(all=False, report=False))),
        ("추천 프로그램 고르기", lambda: cmd_setup(ctx, _ns())),
        ("프로그램 옵션 설정", lambda: cmd_configure(ctx, _ns(program=None))),
        ("패키지 설치", install),
        ("패키지 검색", search),
        ("패키지 제거", remove),
        ("패키지 목록", lambda: cmd_list(ctx, _ns(all=False))),
        ("dotfile 추가", dot_add),
        ("dotfile 목록", lambda: cmd_dot_list(ctx, _ns(all=False))),
        ("dotfile 관리 해제", dot_rm),
        ("변경 미리보기 (적용 안 함)", lambda: cmd_check(ctx, _ns())),
        ("적용", lambda: cmd_apply(ctx, _ns(commit=False, push=False))),
        ("업데이트", lambda: cmd_update(ctx, _ns(inputs=[], no_apply=False))),
        ("세대 목록", lambda: cmd_generations(ctx, _ns())),
        ("이전 세대로 롤백", lambda: cmd_rollback(ctx, _ns())),
        ("현재 상태", lambda: cmd_status(ctx, _ns())),
    ]
    print(ui.bold(f"ehm {__version__}") + ui.dim(f"  —  {MODE_LABELS[ctx.mode]}, {ui.tilde(ctx.home_file)}"))
    while True:
        print()
        idx = ui.choose("무엇을 할까요?", [label for label, _ in actions])
        if idx is None:
            return
        try:
            actions[idx][1]()
        except (ui.EhmError, ModuleShapeError) as e:
            ui.error(str(e))


# ---- argparse -----------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ehm",
        description="Home Manager 를 쉽게 쓰게 해 주는 도구. 인자 없이 실행하면 메뉴가 열립니다.",
    )
    p.add_argument("--version", action="version", version=f"ehm {__version__}")
    p.add_argument("--config", metavar="DIR", help="설정 폴더 지정 (기본값: 자동 감지)")
    p.add_argument("-y", "--yes", action="store_true", help="저장 확인을 건너뜀")
    sub = p.add_subparsers(dest="command", metavar="<명령>")

    def cmd(name, func, help, aliases=(), edits=False):
        c = sub.add_parser(name, help=help, description=help, aliases=list(aliases))
        c.set_defaults(func=func)
        c.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="확인을 건너뜀")
        if edits:
            c.add_argument("--apply", action="store_true", help="저장 후 바로 적용")
            c.add_argument("--backup-conflicts", action="store_true", help="충돌 파일을 백업 폴더로 옮김")
        return c

    cmd("status", cmd_status, "현재 구성과 상태 보기")
    c = cmd("suggest", cmd_suggest, "설정을 분석해 개선안을 제안하고 골라서 적용", edits=True)
    c.add_argument("--report", action="store_true", help="목록만 보여 줌")
    c.add_argument("--all", action="store_true", help="-y 와 함께 쓰면 고칠 수 있는 제안을 전부 선택")
    cmd("setup", cmd_setup, "추천 프로그램을 골라 켜기", edits=True)

    c = cmd("add", cmd_add, "패키지 설치 목록에 추가", aliases=["install"], edits=True)
    c.add_argument("packages", nargs="+")
    c.add_argument("--no-check", action="store_true", help="nixpkgs 존재 확인을 건너뜀")
    c = cmd("remove", cmd_remove, "패키지 제거", aliases=["rm", "uninstall"], edits=True)
    c.add_argument("packages", nargs="+")
    c = cmd("list", cmd_list, "패키지 목록", aliases=["ls"])
    c.add_argument("--all", action="store_true", help="모듈이 설치한 것까지 전부 (평가 필요)")
    c = cmd("search", cmd_search, "nixpkgs 검색")
    c.add_argument("terms", nargs="+")
    c.add_argument("--limit", type=int, default=25)

    dot = sub.add_parser("dot", help="dotfile 관리", description="dotfile 관리")
    dot_sub = dot.add_subparsers(dest="dot_command", metavar="<명령>", required=True)

    def dcmd(name, func, help, aliases=(), edits=False):
        c = dot_sub.add_parser(name, help=help, description=help, aliases=list(aliases))
        c.set_defaults(func=func)
        c.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="확인을 건너뜀")
        if edits:
            c.add_argument("--apply", action="store_true", help="저장 후 바로 적용")
            c.add_argument("--backup-conflicts", action="store_true", help="충돌 파일을 백업 폴더로 옮김")
        return c

    c = dcmd("add", cmd_dot_add, "파일/폴더를 저장소로 옮겨 관리 시작", edits=True)
    c.add_argument("paths", nargs="+")
    c.add_argument("--copy", action="store_true", help="링크 대신 빌드 시 복사 (읽기 전용, 수정 후 적용 필요)")
    c.add_argument("--allow-secrets", action="store_true", help="비밀 정보 검사 결과를 무시")
    c = dcmd("rm", cmd_dot_rm, "관리 해제. 파일은 일반 파일로 되돌림", aliases=["remove"], edits=True)
    c.add_argument("targets", nargs="+")
    c.add_argument("--delete", action="store_true", help="되돌리지 않고 파일도 없앰 (저장소 복사본은 백업됨)")
    c = dcmd("list", cmd_dot_list, "관리 중인 dotfile 과 상태", aliases=["ls"])
    c.add_argument("--all", action="store_true", help="programs.* 가 생성한 파일까지 표시")
    c = dcmd("edit", cmd_dot_edit, "dotfile 원본을 편집기로 열기")
    c.add_argument("target")

    c = cmd("programs", cmd_programs, "Home Manager 모듈이 있는 프로그램 찾기")
    c.add_argument("query", nargs="?")
    c = cmd("configure", cmd_configure, "프로그램 옵션을 보고 골라서 설정", aliases=["conf"], edits=True)
    c.add_argument("program", nargs="?")
    c = cmd("enable", cmd_enable, "programs.<이름> 켜기", edits=True)
    c.add_argument("names", nargs="+")
    c = cmd("disable", cmd_disable, "programs.<이름> 끄기", edits=True)
    c.add_argument("names", nargs="+")
    c = cmd("set", cmd_set, "옵션 값 설정 (예: ehm set programs.git.settings.user.name jihoo)", edits=True)
    c.add_argument("option")
    c.add_argument("value")
    c.add_argument("--nix", action="store_true", help="값을 Nix 표현식 그대로 사용")
    c.add_argument("--str", action="store_true", help="값을 항상 문자열로 사용")
    c = cmd("unset", cmd_unset, "옵션 선언 지우기", edits=True)
    c.add_argument("option")
    c = cmd("get", cmd_get, "옵션 값 보기")
    c.add_argument("option")

    c = cmd("apply", cmd_apply, "설정 적용 (빌드 → 미리보기 → 충돌 처리 → 적용)", aliases=["switch"])
    c.add_argument("--backup-conflicts", action="store_true", help="충돌 파일을 백업 폴더로 옮김")
    c.add_argument("--commit", action="store_true", help="적용 후 git 커밋")
    c.add_argument("--push", action="store_true", help="커밋 후 푸시 (--commit 과 함께)")
    cmd("check", cmd_check, "빌드와 변경 미리보기만 (sudo 불필요)", aliases=["preview"])
    c = cmd("update", cmd_update, "flake 입력을 최신으로 올리고 적용")
    c.add_argument("inputs", nargs="*", help="특정 입력만 업데이트")
    c.add_argument("--no-apply", action="store_true")
    c.add_argument("--backup-conflicts", action="store_true")
    cmd("generations", cmd_generations, "세대 목록", aliases=["gens"])
    cmd("rollback", cmd_rollback, "이전 세대로 되돌리기")
    c = cmd("gc", cmd_gc, "오래된 세대와 쓰지 않는 패키지 정리")
    c.add_argument("--older-than", default="14d", help="기본값: 14d")
    cmd("edit", cmd_edit, "설정 파일을 편집기로 열기")
    sub.add_parser("config", help="ehm 자체 설정 안내")
    c = sub.add_parser("init", help="새 Home Manager flake 설정 만들기")
    c.add_argument("--dir", help="기본값: ~/.config/home-manager")
    c.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="확인을 건너뜀")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui.ASSUME_YES = args.yes
    try:
        if args.command == "init":
            cmd_init(args)
            return 0
        if args.command == "config":
            cmd_config(args.config)
            return 0
        ctx = detect(args.config)
        if args.command is None:
            menu(ctx)
            return 0
        args.func(ctx, args)
        return 0
    except ui.EhmError as e:
        ui.error(str(e))
        return 1
    except ModuleShapeError as e:
        ui.error(f"{e}\n`ehm edit` 으로 직접 수정해 주세요.")
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
