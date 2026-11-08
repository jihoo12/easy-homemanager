"""Detect how Home Manager is set up on this machine and run Nix commands against it."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .nixfile import NixParseError, tokenize
from .ui import EhmError

#: Base ``nix`` invocation; enables flakes regardless of the user's nix.conf.
NIX = ["nix", "--extra-experimental-features", "nix-command flakes", "--option", "warn-dirty", "false"]

MODE_LABELS = {
    "nixos-flake": "NixOS module + flake",
    "nixos-channel": "NixOS module + channels",
    "hm-flake": "standalone Home Manager + flake",
    "hm-channel": "standalone Home Manager + channels",
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
        raise EhmError(f"Could not read {path}: {e}") from e


def _strip_comments(text: str) -> str:
    """Drop comments so that commented-out code does not affect detection."""
    try:
        return " ".join(t.text for t in tokenize(text))
    except NixParseError:
        return text


def _attr_regex(text: str) -> str:
    """Undo the spaces ``_strip_comments`` inserts around dots in attribute paths."""
    return re.sub(r"\s*\.\s*", ".", text)


@dataclass
class Context:
    config_dir: Path
    mode: str
    home_file: Path
    user: str
    host: str | None = None  # attribute name in nixosConfigurations / homeConfigurations
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
            raise EhmError("This feature requires a flake-based configuration.")

    def inputs_args(self) -> list[str]:
        """Arguments that pin ``nixpkgs`` to the revision in the config's flake.lock."""
        return ["--inputs-from", str(self.config_dir)] if self.is_flake else []

    # ---- nix invocations -----------------------------------------------
    def nix_eval_json(self, attr: str, apply: str | None = None):
        cmd = [*NIX, "eval", "--json", attr]
        if apply:
            cmd += ["--apply", apply]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise EhmError("Nix evaluation failed:\n" + last_error(r.stderr))
        return json.loads(r.stdout)

    def package_info(self, name: str) -> dict | None:
        """``{name, description}`` for a nixpkgs attribute, or ``None`` if it does not exist."""
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
            f"Could not look up package '{name}' (possibly a network problem):\n"
            + last_error(r.stderr)
            + "\nUse --no-check to skip this check."
        )

    def search_packages(self, terms: list[str]) -> list[tuple[str, str, str]]:
        """``(attribute, version, description)`` matches from ``nix search``, best first."""
        cmd = [*NIX, "search", *self.inputs_args(), "nixpkgs", *terms, "--json"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise EhmError("Search failed:\n" + last_error(r.stderr))
        rows = []
        for key, value in json.loads(r.stdout or "{}").items():
            # Keys look like "legacyPackages.<system>.<attr>".
            attr = key.split(".", 2)[2] if key.count(".") >= 2 else key
            rows.append((attr, value.get("version", ""), value.get("description", "")))
        needle = terms[0].lower() if terms else ""
        rows.sort(key=lambda r: (r[0].lower() != needle, not r[0].lower().startswith(needle), len(r[0]), r[0]))
        return rows

    def program_modules(self) -> list[str]:
        """Names of all programs that have a ``programs.<name>.enable`` option."""
        expr = (
            "cfg: builtins.filter (n: "
            "let t = builtins.tryEval (cfg.programs.${n} ? enable); in t.success && t.value) "
            "(builtins.attrNames cfg.programs)"
        )
        return self.nix_eval_json(self.hm_config_attr(), expr)


def last_error(stderr: str, lines: int = 12) -> str:
    """The last few non-blank lines of a command's stderr."""
    kept = [line for line in stderr.strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def _candidate_dirs(settings: dict, override: str | None) -> list[Path]:
    explicit = override or os.environ.get("EHM_CONFIG_DIR") or settings.get("config_dir")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir():
            raise EhmError(f"Configuration directory does not exist: {path}")
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
            # Home Manager may be wired up in a module imported from another file.
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
        raise EhmError(f"Could not read {kind} from the flake:\n" + last_error(r.stderr))
    return json.loads(r.stdout)


def _pick_name(names: list[str], preferred: list[str], kind: str) -> str:
    for p in preferred:
        if p in names:
            return p
    if len(names) == 1:
        return names[0]
    raise EhmError(
        f"Multiple {kind} found: {', '.join(names)}\n"
        f'Set host = "<name>" in {settings_path()}.'
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
    """Locate the user's Home Manager configuration and build a :class:`Context`."""
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
            raise EhmError(f"Unknown mode in settings: {mode}")
        home_setting = settings.get("home_file")
        home = (
            (directory / home_setting).resolve() if home_setting
            else _find_home_file(directory, mode, user)
        )
        if home is None or not home.exists():
            raise EhmError(
                f"Could not find the Home Manager configuration file in {directory}.\n"
                f'Set it explicitly, e.g. home_file = "home.nix", in {settings_path()}.'
            )
        host = settings.get("host")
        if mode == "nixos-flake" and not host:
            host = _pick_name(_flake_names(directory, "nixosConfigurations"), [socket.gethostname()], "nixosConfigurations")
        elif mode == "hm-flake" and not host:
            hostname = socket.gethostname()
            host = _pick_name(_flake_names(directory, "homeConfigurations"), [f"{user}@{hostname}", user], "homeConfigurations")
        return Context(directory, mode, home, user, host, settings)
    raise EhmError(
        "No Home Manager configuration found. Searched: " + ", ".join(tried)
        + "\nRun `ehm init` to create one."
    )
