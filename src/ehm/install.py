"""Install ehm itself through the user's flake (a special case of adding an input)."""

from __future__ import annotations

import shutil

from . import inputs
from .context import Context
from .workspace import Session

DEFAULT_URL = "github:jihoo12/easy-homemanager"


def flake_url(ctx: Context) -> str:
    return ctx.settings.get("ehm_flake_url", DEFAULT_URL)


def is_installed(ctx: Context, session: Session) -> bool:
    """Whether the configuration already provides ehm (or it is on ``PATH`` some other way)."""
    if shutil.which("ehm"):
        return True
    if not (ctx.config_dir / "flake.nix").is_file():
        return False
    flake = session.load(ctx.config_dir / "flake.nix")
    return flake.has_path(("inputs", "ehm")) or "easy-homemanager" in flake.text


def problem(ctx: Context, session: Session) -> str | None:
    """Why ehm cannot install itself automatically, or ``None`` if it can."""
    if not ctx.is_flake:
        return "Installing ehm through the configuration requires a flake-based setup."
    return inputs.wiring_problem(ctx, session, retry="run `ehm self-install` again")


def plan_install(ctx: Context, session: Session) -> None:
    """Stage ehm as a flake input and add its package to ``home.packages``."""
    inputs.plan_add(ctx, session, "ehm", flake_url(ctx), package="default")
    session.notes.append("Afterwards `ehm update` keeps ehm up to date.")
