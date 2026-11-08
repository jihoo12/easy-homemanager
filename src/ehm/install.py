"""Install ehm itself through the user's flake.

Three edits are staged:

1. ``flake.nix``: a new ``ehm`` input (following the config's ``nixpkgs`` when present),
   adding ``ehm`` to the ``outputs`` argument pattern if it has no ``...``;
2. the home module: an ``inputs`` argument, if Home Manager modules receive it through
   ``extraSpecialArgs`` but the module does not declare it yet;
3. ``home.packages``: ``inputs.ehm.packages.${pkgs.stdenv.hostPlatform.system}.default``.

When Home Manager modules do not receive ``inputs`` at all, the wiring lives inside
``outputs`` (an expression ehm does not rewrite), so the user is told what to add.
"""

from __future__ import annotations

import re
import shutil

from .context import Context, _attr_regex, _strip_comments
from .nixfile import NixFile
from .ui import EhmError
from .workspace import PACKAGES, Session

DEFAULT_URL = "github:jihoo12/easy-homemanager"
PACKAGE_EXPR = "inputs.ehm.packages.${pkgs.stdenv.hostPlatform.system}.default"
_PASSES_INPUTS = re.compile(
    r"extraSpecialArgs\s*=\s*(inputs\b|\{[^}]*(\binherit\b[^;]*\binputs\b|\binputs\s*=))"
)


def flake_url(ctx: Context) -> str:
    return ctx.settings.get("ehm_flake_url", DEFAULT_URL)


def _flake(ctx: Context, session: Session) -> NixFile:
    return session.load(ctx.config_dir / "flake.nix")


def is_installed(ctx: Context, session: Session) -> bool:
    """Whether the configuration already provides ehm (or it is on ``PATH`` some other way)."""
    if shutil.which("ehm"):
        return True
    if not (ctx.config_dir / "flake.nix").is_file():
        return False
    flake = _flake(ctx, session)
    return flake.has_path(("inputs", "ehm")) or "easy-homemanager" in flake.text


def problem(ctx: Context, session: Session) -> str | None:
    """Why ehm cannot install itself automatically, or ``None`` if it can."""
    if not ctx.is_flake:
        return "Installing ehm through the configuration requires a flake-based setup."
    flake = _flake(ctx, session)
    if flake.shape_error:
        return f"flake.nix cannot be edited automatically: {flake.shape_error}"
    if flake.find(("outputs",)) is None:
        return "flake.nix has no `outputs` definition that ehm can find."
    home = session.file_with_packages()
    if home.shape_error:
        return f"{home.path.name} cannot be edited automatically: {home.shape_error}"
    if "inputs" in home.arg_names():
        return None
    if not _PASSES_INPUTS.search(_attr_regex(_strip_comments(flake.text))):
        snippet = (
            "home-manager.extraSpecialArgs = { inherit inputs; };" if ctx.is_nixos
            else "extraSpecialArgs = { inherit inputs; };  # in homeManagerConfiguration"
        )
        return (
            "Home Manager modules do not receive the flake's inputs. Bind them in `outputs` "
            "(e.g. `outputs = { self, nixpkgs, ... }@inputs:`), add\n"
            f"  {snippet}\n"
            "to flake.nix, and run `ehm self-install` again."
        )
    return None


def _ensure_outputs_accepts_ehm(flake: NixFile) -> NixFile:
    """Flakes call ``outputs`` with every input; a closed pattern must name the new one."""
    b = flake.find(("outputs",))
    if b is None or not flake._is(b.value_tok, "{"):
        return flake  # `outputs = inputs: ...` accepts anything
    close = flake.match[b.value_tok]
    names = [t.text for t in flake.tokens[b.value_tok + 1 : close]]
    if "..." in names or "ehm" in names:
        return flake
    if close == b.value_tok + 1:  # `{ }:`
        at = flake.tokens[b.value_tok].end
        return flake._replaced(at, at, " ehm")
    last = flake.tokens[close - 1]
    return flake._replaced(last.end, last.end, " ehm" if last.text == "," else ", ehm")


def plan_install(ctx: Context, session: Session) -> None:
    """Stage the edits that install ehm through the flake (see the module docstring)."""
    reason = problem(ctx, session)
    if reason:
        raise EhmError(reason)
    url = flake_url(ctx)
    flake = _flake(ctx, session)
    if flake.has_path(("inputs", "nixpkgs")):
        expr = f'{{\n{{INDENT}}  url = "{url}";\n{{INDENT}}  inputs.nixpkgs.follows = "nixpkgs";\n{{INDENT}}}}'
        flake = flake.set_expr(("inputs", "ehm"), expr)
    else:
        flake = flake.set_expr(("inputs", "ehm", "url"), f'"{url}"')
    session.save(_ensure_outputs_accepts_ehm(flake))

    target = session.file_with_packages().ensure_arg("inputs")
    session.save(target.add_list_item(PACKAGES, PACKAGE_EXPR, raw=True))
    session.notes.append(
        f"The next build adds `ehm` to flake.lock, which needs access to {url}. "
        "Afterwards `ehm update` keeps it up to date."
    )
