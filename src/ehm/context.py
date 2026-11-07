"""이 컴퓨터의 Home Manager 구성 방식을 찾아내고 nix 명령을 실행한다."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .nixfile import tokenize
from .ui import EhmError

NIX = ["nix", "--extra-experimental-features", "nix-command flakes", "--option", "warn-dirty", "false"]

MODE_LABELS = {
    "nixos-flake": "NixOS 모듈 + flake",
    "nixos-channel": "NixOS 모듈 + 채널",
    "hm-flake": "독립 실행 Home Manager + flake",
    "hm-channel": "독립 실행 Home Manager + 채널",
}


def xdg_dir(env: str, fallback: str) -> Path:
    value = os.environ.get(env)
    return Path(value) if value else Path.home() / fallback


def state_dir() -> Path:
    return xdg_dir("XDG_STATE_HOME", ".local/state") / "ehm"


def settings_path() -> Path:
    return xdg_dir("XDG_CONFIG_HOME", ".config") / "ehm" / "config.toml"


def load_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise EhmError(f"{path} 를 읽지 못했습니다: {e}") from e


def _strip_comments(text: str) -> str:
    """주석에 적힌 내용에 속지 않도록 주석을 제외한 코드만 남긴다."""
    try:
        return " ".join(t.text for t in tokenize(text))
    except Exception:
        return text


def _attr_regex(text: str) -> str:
    # 토큰 사이에 공백을 넣어 이어 붙였으므로 점 주변 공백을 없앤다.
    return re.sub(r"\s*\.\s*", ".", text)


@dataclass
class Context:
    config_dir: Path
    mode: str
    home_file: Path
    user: str
    host: str | None = None  # nixosConfigurations 또는 homeConfigurations 의 이름
    settings: dict = field(default_factory=dict)

    @property
    def is_flake(self) -> bool:
        return self.mode.endswith("-flake")

    @property
    def is_nixos(self) -> bool:
        return self.mode.startswith("nixos")

    @property
    def dotfiles_dir(self) -> Path:
        return self.config_dir / self.settings.get("dotfiles_dir", "dotfiles")

    @property
    def extra_args(self) -> list[str]:
        return list(self.settings.get("extra_rebuild_args", []))

    def flake_system(self) -> str:
        kind = "nixosConfigurations" if self.is_nixos else "homeConfigurations"
        return f'{self.config_dir}#{kind}."{self.host}"'

    def hm_config_attr(self) -> str:
        self.require_flake()
        if self.is_nixos:
            return f'{self.flake_system()}.config.home-manager.users."{self.user}"'
        return f"{self.flake_system()}.config"

    def activation_attr(self) -> str:
        return f"{self.hm_config_attr()}.home.activationPackage"

    def require_flake(self) -> None:
        if not self.is_flake:
            raise EhmError("이 기능은 flake 구성에서만 지원합니다.")

    def inputs_args(self) -> list[str]:
        """flake.lock 에 고정된 nixpkgs 를 쓰기 위한 인자."""
        return ["--inputs-from", str(self.config_dir)] if self.is_flake else []

    # ---- nix 호출 ----------------------------------------------------
    def nix_eval_json(self, attr: str, apply: str | None = None):
        cmd = [*NIX, "eval", "--json", attr]
        if apply:
            cmd += ["--apply", apply]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise EhmError("nix 평가에 실패했습니다:\n" + last_error(r.stderr))
        return json.loads(r.stdout)

    def package_info(self, name: str) -> dict | None:
        """패키지가 있으면 {name, description}, 없으면 None."""
        expr = (
            "p: { name = p.name or (p.pname or \"\"); "
            "description = (p.meta or {}).description or \"\"; }"
        )
        if self.is_flake:
            cmd = [*NIX, "eval", "--json", *self.inputs_args(), f"nixpkgs#{name}", "--apply", expr]
        else:
            cmd = [*NIX, "eval", "--json", "-f", "<nixpkgs>", name, "--apply", expr]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return json.loads(r.stdout)
        if re.search(r"does not provide attribute|attribute '.*' (?:missing|not found)|cannot find flake attribute", r.stderr):
            return None
        raise EhmError(
            f"패키지 '{name}' 을(를) 확인하지 못했습니다 (네트워크 문제일 수 있음):\n"
            + last_error(r.stderr)
            + "\n확인을 건너뛰려면 --no-check 를 쓰세요."
        )

    def program_modules(self) -> list[str]:
        """`programs.<이름>.enable` 옵션이 있는 프로그램 이름 목록."""
        expr = (
            "cfg: builtins.filter (n: "
            "let t = builtins.tryEval (cfg.programs.${n} ? enable); in t.success && t.value) "
            "(builtins.attrNames cfg.programs)"
        )
        return self.nix_eval_json(self.hm_config_attr(), expr)


def last_error(stderr: str, lines: int = 12) -> str:
    kept = [l for l in stderr.strip().splitlines() if l.strip()]
    return "\n".join(kept[-lines:])


def _candidate_dirs(settings: dict, override: str | None) -> list[Path]:
    explicit = override or os.environ.get("EHM_CONFIG_DIR") or settings.get("config_dir")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir():
            raise EhmError(f"설정 폴더가 없습니다: {path}")
        return [path]
    config_home = xdg_dir("XDG_CONFIG_HOME", ".config")
    return [config_home / "home-manager", config_home / "nixpkgs", Path("/etc/nixos")]


def _detect_mode(directory: Path) -> str | None:
    flake = directory / "flake.nix"
    if flake.exists():
        code = _attr_regex(_strip_comments(flake.read_text()))
        if re.search(r"home-manager\.(nixosModules|users)|nixosModules\.home-manager", code):
            return "nixos-flake"
        if "homeConfigurations" in code:
            return "hm-flake"
        if "nixosConfigurations" in code:
            # 모듈을 다른 파일에서 불러오는 경우
            for nix in directory.rglob("*.nix"):
                if ".git" in nix.parts:
                    continue
                text = _attr_regex(_strip_comments(nix.read_text(errors="replace")))
                if re.search(r"home-manager\.users|home-manager\s*=\s*\{", text):
                    return "nixos-flake"
        return None
    if (directory / "configuration.nix").exists():
        text = _attr_regex(_strip_comments((directory / "configuration.nix").read_text()))
        if "home-manager" in text:
            return "nixos-channel"
        return None
    if (directory / "home.nix").exists():
        return "hm-channel"
    return None


def _flake_names(directory: Path, kind: str) -> list[str]:
    code = _attr_regex(_strip_comments((directory / "flake.nix").read_text()))
    found = re.findall(kind + r'\."?([\w@.\-]+?)"?\s*=', code)
    if found:
        return list(dict.fromkeys(found))
    r = subprocess.run(
        [*NIX, "eval", "--json", f"{directory}#{kind}", "--apply", "builtins.attrNames"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise EhmError(f"flake 의 {kind} 를 읽지 못했습니다:\n" + last_error(r.stderr))
    return json.loads(r.stdout)


def _pick_name(names: list[str], preferred: list[str], kind: str) -> str:
    for p in preferred:
        if p in names:
            return p
    if len(names) == 1:
        return names[0]
    raise EhmError(
        f"{kind} 가 여러 개입니다: {', '.join(names)}\n"
        f"{settings_path()} 에 host = \"이름\" 을 지정해 주세요."
    )


def _find_home_file(directory: Path, mode: str, user: str) -> Path | None:
    sources = []
    for name in ("flake.nix", "configuration.nix"):
        p = directory / name
        if p.exists():
            sources.append(_attr_regex(_strip_comments(p.read_text())))
    patterns = [
        rf'users\."?{re.escape(user)}"?\s*=\s*(?:import\s+)?(\.{{1,2}}/[^\s;\]]+)',
        r"modules\s*=\s*\[[^\]]*?(\./[^\s\]]+)" if mode == "hm-flake" else None,
    ]
    for pat in filter(None, patterns):
        for code in sources:
            m = re.search(pat, code)
            if m:
                path = (directory / m.group(1)).resolve()
                if path.is_dir():
                    path = path / "default.nix"
                if path.exists():
                    return path
    for rel in ("home.nix", "home/default.nix", "home/home.nix", f"users/{user}/home.nix", f"home/{user}.nix"):
        if (directory / rel).exists():
            return directory / rel
    return None


def detect(override: str | None = None) -> Context:
    settings = load_settings()
    user = settings.get("user") or os.environ.get("USER") or Path.home().name
    tried = []
    for directory in _candidate_dirs(settings, override):
        tried.append(str(directory))
        if not directory.is_dir():
            continue
        mode = settings.get("mode") or _detect_mode(directory)
        if not mode:
            continue
        if mode not in MODE_LABELS:
            raise EhmError(f"알 수 없는 mode 값: {mode}")
        home_setting = settings.get("home_file")
        home = (
            (directory / home_setting).resolve() if home_setting
            else _find_home_file(directory, mode, user)
        )
        if home is None or not home.exists():
            raise EhmError(
                f"{directory} 에서 Home Manager 설정 파일을 찾지 못했습니다.\n"
                f"{settings_path()} 에 home_file = \"home.nix\" 처럼 지정해 주세요."
            )
        host = settings.get("host")
        if mode == "nixos-flake" and not host:
            host = _pick_name(_flake_names(directory, "nixosConfigurations"), [socket.gethostname()], "nixosConfigurations")
        elif mode == "hm-flake" and not host:
            hostname = socket.gethostname()
            host = _pick_name(_flake_names(directory, "homeConfigurations"), [f"{user}@{hostname}", user], "homeConfigurations")
        return Context(directory, mode, home, user, host, settings)
    raise EhmError(
        "Home Manager 설정을 찾지 못했습니다. 찾아본 곳: " + ", ".join(tried)
        + "\n새로 시작하려면 `ehm init` 을 실행하세요."
    )
