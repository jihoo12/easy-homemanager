"""Flake inputs: external sources of packages and Home Manager modules.

Listing combines ``flake.nix`` (what is declared), ``flake.lock`` (what is pinned) and
the configuration's modules (where an input is used). Adding inspects what a flake
offers for this system and stages the input together with a package in
``home.packages`` and/or a module in ``imports``. Removing stages the reverse, and
refuses when the input is used somewhere ehm cannot rewrite.

Home Manager modules reach inputs through the ``inputs`` module argument, which the
flake must pass via ``extraSpecialArgs``; :func:`wiring_problem` explains what is
missing when it does not.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

from .context import NIX, Context, _attr_regex, _strip_comments, last_error
from .nixfile import NixFile, format_key, nix_string
from .ui import EhmError
from .workspace import PACKAGES, Session

_PASSES_INPUTS = re.compile(
    r"extraSpecialArgs\s*=\s*(inputs\b|\{[^}]*(\binherit\b[^;]*\binputs\b|\binputs\s*=))"
)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_'-]*$")
_GITHUB_URL = re.compile(r"^https?://(github|gitlab)\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$")
SYSTEM = "${pkgs.stdenv.hostPlatform.system}"


@dataclass
class FlakeInput:
    name: str
    url: str
    follows: dict[str, str] = field(default_factory=dict)  # dependency → input it follows
    rev: str | None = None
    locked_at: datetime | None = None
    own_nixpkgs: bool = False  # pins its own nixpkgs instead of following ours
    used_by: list[str] = field(default_factory=list)  # e.g. "home.nix: imports"


@dataclass
class Offer:
    """What a flake provides for the current system."""

    packages: list[str]
    home_modules: dict[str, list[str]]  # output attribute (homeModules, ...) → module names
    has_nixpkgs: bool


# ---- helpers ----------------------------------------------------------------


def _flake(ctx: Context, session: Session) -> NixFile:
    ctx.require_flake()
    return session.load(ctx.config_dir / "flake.nix")


def _reference(name: str) -> re.Pattern:
    """``inputs.<name>`` (or ``inputs."<name>"``) in Nix source."""
    return re.compile(rf"\binputs\s*\.\s*(?:{re.escape(name)}|\"{re.escape(name)}\")(?![\w'-])")


def normalize_url(url: str) -> str:
    """Accept a web URL of a GitHub/GitLab repository as well as a flake reference."""
    url = url.strip()
    m = _GITHUB_URL.match(url)
    return f"{m.group(1)}:{m.group(2)}/{m.group(3)}" if m else url


def suggest_name(url: str) -> str:
    """A default input name: the repository name, made a valid Nix identifier."""
    tail = re.split(r"[/:]", normalize_url(url).split("?")[0].rstrip("/"))[-1]
    tail = re.sub(r"\.git$", "", tail)
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", tail).strip("-").lower()
    return name if name and _NAME.match(name) else "input"


def package_expr(name: str, package: str) -> str:
    return f"inputs.{format_key(name)}.packages.{SYSTEM}.{format_key(package)}"


def module_expr(name: str, attribute: str, module: str) -> str:
    return f"inputs.{format_key(name)}.{attribute}.{format_key(module)}"


# ---- listing ------------------------------------------------------------------


def _lock(ctx: Context) -> dict:
    path = ctx.config_dir / "flake.lock"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _locked_url(original: dict) -> str:
    kind = original.get("type")
    if kind in ("github", "gitlab", "sourcehut"):
        ref = f"/{original['ref']}" if original.get("ref") else ""
        return f"{kind}:{original.get('owner')}/{original.get('repo')}{ref}"
    return original.get("url") or original.get("path") or kind or ""


def list_inputs(ctx: Context, session: Session) -> list[FlakeInput]:
    flake = _flake(ctx, session)
    found: dict[str, FlakeInput] = {}
    for b in flake.bindings:
        if len(b.path) < 2 or b.path[0] != "inputs" or b.attrs_open is not None:
            continue
        item = found.setdefault(b.path[1], FlakeInput(b.path[1], ""))
        value = flake.value_text(b).strip()
        if b.path[2:] in ((), ("url",)) and value.startswith('"'):
            item.url = value.strip('"')
        elif len(b.path) == 5 and b.path[2] == "inputs" and b.path[4] == "follows":
            item.follows[b.path[3]] = value.strip('"')

    nodes = _lock(ctx).get("nodes", {})
    for name, key in nodes.get("root", {}).get("inputs", {}).items():
        if not isinstance(key, str) or key not in nodes:
            continue
        item = found.setdefault(name, FlakeInput(name, ""))
        node = nodes[key]
        locked = node.get("locked", {})
        item.rev = locked.get("rev")
        if locked.get("lastModified"):
            item.locked_at = datetime.fromtimestamp(locked["lastModified"])
        item.url = item.url or _locked_url(node.get("original", {}))
        item.own_nixpkgs = isinstance(node.get("inputs", {}).get("nixpkgs"), str)

    for item in found.values():
        item.used_by = _usages(ctx, session, item.name)
    return sorted(found.values(), key=lambda i: i.name)


def _usages(ctx: Context, session: Session, name: str) -> list[str]:
    """Where the configuration refers to ``inputs.<name>`` (outside the input declaration)."""
    pattern = _reference(name)
    out = []
    for nf in session.modules():
        for b in nf.bindings:
            if b.attrs_open is None and pattern.search(nf.value_text(b)):
                out.append(f"{nf.path.name}: {'.'.join(b.path)}")
    return out + _other_usages(ctx, session, name)


def _other_usages(ctx: Context, session: Session, name: str) -> list[str]:
    """Uses outside the Home Manager modules: in ``outputs`` and other ``.nix`` files."""
    pattern = _reference(name)
    out = []
    flake = session.load(ctx.config_dir / "flake.nix")
    outputs = flake.find(("outputs",))
    if outputs is not None:
        body = _outputs_body(flake, outputs)
        if pattern.search(body) or re.search(rf"(?<![\w.'-]){re.escape(name)}\s*\.", body):
            out.append("flake.nix: outputs")
    loaded = {nf.path for nf in session.modules()} | {flake.path}
    for nix in sorted(ctx.config_dir.rglob("*.nix")):
        if nix in loaded or ".git" in nix.relative_to(ctx.config_dir).parts:
            continue
        if pattern.search(_strip_comments(nix.read_text(errors="replace"))):
            out.append(str(nix.relative_to(ctx.config_dir)))
    return out


def _outputs_body(flake: NixFile, outputs) -> str:
    """The text of ``outputs`` after its argument pattern, without comments."""
    start = outputs.value_tok
    if flake._is(start, "{"):
        start = flake.match[start] + 1
    while start < outputs.end_tok and not flake._is(start, ":"):
        start += 1
    text = flake.text[flake.tokens[min(start + 1, outputs.end_tok)].start : outputs.value_end]
    return _attr_regex(_strip_comments(text))


# ---- inspecting a flake -----------------------------------------------------------

_INSPECT = """
let
  f = builtins.getFlake @URL@;
  sys = builtins.currentSystem;
  names = v: let t = builtins.tryEval v; in
    if t.success && builtins.isAttrs t.value then builtins.attrNames t.value else [];
in {
  packages = names (f.packages.${sys} or {});
  homeModules = names (f.homeModules or {});
  homeManagerModules = names (f.homeManagerModules or {});
  hasNixpkgs = (f.inputs or {}) ? nixpkgs;
}
"""


def inspect(url: str) -> Offer:
    """Fetch a flake and list the packages and Home Manager modules it offers."""
    expr = _INSPECT.replace("@URL@", nix_string(normalize_url(url)))
    r = subprocess.run([*NIX, "eval", "--impure", "--json", "--expr", expr], capture_output=True, text=True)
    if r.returncode != 0:
        raise EhmError(f"Could not read the flake {url}:\n" + last_error(r.stderr))
    data = json.loads(r.stdout)
    modules = {attr: data[attr] for attr in ("homeModules", "homeManagerModules") if data[attr]}
    return Offer(data["packages"], modules, data["hasNixpkgs"])


# ---- adding ---------------------------------------------------------------------


def wiring_problem(ctx: Context, session: Session, retry: str = "try again") -> str | None:
    """Why inputs cannot be used from Home Manager modules automatically, or ``None``."""
    if not ctx.is_flake:
        return "Flake inputs require a flake-based setup."
    flake = _flake(ctx, session)
    if flake.shape_error:
        return f"flake.nix cannot be edited automatically: {flake.shape_error}"
    if flake.find(("outputs",)) is None:
        return "flake.nix has no `outputs` definition that ehm can find."
    home = session.file_with_packages()
    if home.shape_error:
        return f"{home.path.name} cannot be edited automatically: {home.shape_error}"
    if "inputs" in home.arg_names() or _PASSES_INPUTS.search(_attr_regex(_strip_comments(flake.text))):
        return None
    snippet = (
        "home-manager.extraSpecialArgs = { inherit inputs; };" if ctx.is_nixos
        else "extraSpecialArgs = { inherit inputs; };  # in homeManagerConfiguration"
    )
    return (
        "Home Manager modules do not receive the flake's inputs. Bind them in `outputs` "
        "(e.g. `outputs = { self, nixpkgs, ... }@inputs:`), add\n"
        f"  {snippet}\n"
        f"to flake.nix, and {retry}."
    )


def _outputs_pattern(flake: NixFile) -> tuple[int, int] | None:
    """Token indices of the ``{`` and ``}`` of the ``outputs`` argument pattern."""
    b = flake.find(("outputs",))
    if b is None or not flake._is(b.value_tok, "{"):
        return None  # `outputs = inputs: ...` accepts anything
    return b.value_tok, flake.match[b.value_tok]


def _accept_in_outputs(flake: NixFile, name: str) -> NixFile:
    """Flakes call ``outputs`` with every input; a closed pattern must name a new one."""
    pattern = _outputs_pattern(flake)
    if pattern is None:
        return flake
    open_, close = pattern
    names = [t.text for t in flake.tokens[open_ + 1 : close]]
    if "..." in names or name in names:
        return flake
    if close == open_ + 1:  # `{ }:`
        at = flake.tokens[open_].end
        return flake._replaced(at, at, f" {name}")
    last = flake.tokens[close - 1]
    return flake._replaced(last.end, last.end, f" {name}" if last.text == "," else f", {name}")


def _drop_from_outputs(flake: NixFile, name: str) -> NixFile:
    """Remove ``name`` from a closed ``outputs`` pattern (a removed input is not passed)."""
    pattern = _outputs_pattern(flake)
    if pattern is None:
        return flake
    open_, close = pattern
    for i in range(open_ + 1, close):
        t = flake.tokens[i]
        if t.kind != "ident" or t.text != name or not (flake._is(i - 1, "{") or flake._is(i - 1, ",")):
            continue
        if flake._is(i + 1, ","):
            end = flake.tokens[i + 2].start if i + 2 <= close else flake.tokens[i + 1].end
            return flake._replaced(t.start, end, "")
        if flake._is(i - 1, ","):
            return flake._replaced(flake.tokens[i - 1].start, t.end, "")
        return flake._replaced(t.start, t.end, "")
    return flake


def plan_add(
    ctx: Context, session: Session, name: str, url: str, *,
    package: str | None = None, module: tuple[str, str] | None = None, follow_nixpkgs: bool = True,
) -> None:
    """Stage a new input, plus ``home.packages``/``imports`` entries for what it provides.

    ``module`` is ``(attribute, name)``, e.g. ``("homeModules", "default")``.
    """
    if not _NAME.match(name):
        raise EhmError(f"Invalid input name: {name!r}")
    reason = wiring_problem(ctx, session)
    if reason:
        raise EhmError(reason)
    url = normalize_url(url)
    flake = _flake(ctx, session)
    if flake.has_path(("inputs", name)):
        raise EhmError(f"flake.nix already has an input named {name}.")
    key = format_key(name)
    if follow_nixpkgs and flake.has_path(("inputs", "nixpkgs")):
        expr = f'{{\n{{INDENT}}  url = {nix_string(url)};\n{{INDENT}}  inputs.nixpkgs.follows = "nixpkgs";\n{{INDENT}}}}'
        flake = flake.set_expr(("inputs", name), expr)
    else:
        flake = flake.set_expr(("inputs", name, "url"), nix_string(url))
    session.save(_accept_in_outputs(flake, key))

    if package:
        target = session.file_with_packages().ensure_arg("inputs")
        session.save(target.add_list_item(PACKAGES, package_expr(name, package), raw=True))
    if module:
        home = session.load(ctx.home_file).ensure_arg("inputs")
        session.save(home.add_list_item(("imports",), module_expr(name, *module), raw=True, packages=False))
    session.notes.append(f"The next build adds `{name}` to flake.lock (fetches {url}).")


# ---- removing ---------------------------------------------------------------------


def plan_remove(ctx: Context, session: Session, name: str) -> list[str]:
    """Stage removing an input and its ``home.packages``/``imports`` entries.

    Returns what was removed. Raises if the input is used anywhere else.
    """
    flake = _flake(ctx, session)
    if not flake.has_path(("inputs", name)):
        raise EhmError(f"flake.nix has no input named {name}.")
    pattern = _reference(name)
    removed: list[str] = []
    blockers: list[str] = []
    for nf in session.modules():
        current = nf
        for b in nf.bindings:
            if b.attrs_open is not None or not pattern.search(nf.value_text(b)):
                continue
            if b.path not in (PACKAGES, ("imports",)):
                blockers.append(f"{nf.path.name}: {'.'.join(b.path)}")
                continue
            for lst in nf.list_info(b):
                for it in lst.items:
                    if not pattern.search(it.name):
                        continue
                    if not it.simple:
                        blockers.append(f"{nf.path.name}: {'.'.join(b.path)} ({it.name})")
                        continue
                    current = current.remove_list_item(b.path, it.name)
                    removed.append(f"{nf.path.name}: {'.'.join(b.path)} → {it.name}")
        if current is not nf:
            session.save(current)
    blockers += _other_usages(ctx, session, name)
    followers = [
        f"flake.nix: inputs.{b.path[1]}.inputs.{b.path[3]}.follows"
        for b in flake.bindings
        if len(b.path) == 5 and b.path[0] == "inputs" and b.path[2] == "inputs" and b.path[4] == "follows"
        and b.path[1] != name and flake.value_text(b).strip().strip('"').split("/")[0] == name
    ]
    blockers += followers
    if blockers:
        raise EhmError(
            f"`{name}` is used in places ehm does not rewrite, so it was not removed:\n  "
            + "\n  ".join(blockers)
            + "\nRemove those uses first (e.g. with `ehm edit`)."
        )
    flake = _drop_from_outputs(flake.unset(("inputs", name)), name)
    session.save(flake)
    removed.append(f"flake.nix: inputs.{name}")
    session.notes.append(f"Removing `{name}`: " + "; ".join(removed))
    return removed


def find_input(ctx: Context, session: Session, name: str) -> FlakeInput:
    for item in list_inputs(ctx, session):
        if item.name == name:
            return item
    raise EhmError(f"flake.nix has no input named {name}.")
