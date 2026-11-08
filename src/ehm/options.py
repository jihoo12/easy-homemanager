"""Read Home Manager option metadata and let the user pick values interactively."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import ui
from .context import Context
from .nixfile import format_path, split_option_path, to_nix
from .workspace import Session

# Nix function that walks the option tree and collects each option's path, type,
# description, allowed values, example and current value.
_OPTIONS_EXPR = r"""
sys: let
  opts = @OPTS@;
  cfg = @CFG@;
  root = @PATH@;
  at = builtins.foldl' (a: k: if builtins.isAttrs a && a ? ${k} then a.${k} else null);
  scalar = v: builtins.isBool v || builtins.isInt v || builtins.isFloat v || builtins.isString v || v == null;
  show = v: let t = builtins.tryEval (
      if scalar v then { value = v; }
      else if builtins.isList v then
        (if builtins.all scalar v then { value = v; } else { summary = "list of ${toString (builtins.length v)}"; })
      else if builtins.isAttrs v then
        (if (v.type or null) == "derivation" then { summary = "package ${v.name or ""}"; }
         else if v == {} then { value = {}; }
         else { summary = "${toString (builtins.length (builtins.attrNames v))} attributes"; })
      else { summary = "function/path"; });
    in if t.success then t.value else { summary = "unreadable"; };
  text = v: if builtins.isString v then v
    else if builtins.isAttrs v && v ? text then v.text else "";
  enumOf = t: let base = if t.name == "nullOr" then t.nestedTypes.elemType else t;
    in if base.name == "enum" then (base.functor.payload.values or null) else null;
  example = o: let t = builtins.tryEval (
      if !(o ? example) then "" else if builtins.isString o.example then o.example
      else if builtins.isAttrs o.example && o.example ? text then o.example.text
      else builtins.toJSON o.example);
    in if t.success then t.value else "";
  isOpt = v: builtins.isAttrs v && (v._type or null) == "option";
  walk = depth: prefix: node: builtins.concatLists (map (n:
    let v = node.${n}; p = prefix ++ [ n ];
    in if builtins.substring 0 1 n == "_" then []
       else if isOpt v then
         (if (v.visible or true) == false || (v.internal or false) then [] else [{
           path = p;
           type = v.type.description or "";
           kind = v.type.name or "";
           description = text (v.description or "");
           values = enumOf v.type;
           example = example v;
           current = show (at cfg p);
         }])
       else if builtins.isAttrs v && depth < 5 then walk (depth + 1) p v
       else []) (builtins.attrNames node));
  start = at opts root;
in if builtins.isAttrs start then walk 0 root start else []
"""


@dataclass
class Option:
    path: tuple[str, ...]
    type: str
    kind: str
    description: str
    values: list | None
    example: str
    current: dict

    @property
    def is_freeform(self) -> bool:
        return self.kind in ("attrs", "attrsOf", "lazyAttrsOf", "submodule") or (
            "attribute set" in self.type and self.kind not in ("listOf",)
        )

    @property
    def current_text(self) -> str:
        if "summary" in self.current:
            return self.current["summary"]
        value = self.current.get("value")
        if value is None:
            return "(unset)"
        if value is True:
            return "on"
        if value is False:
            return "off"
        return json.dumps(value, ensure_ascii=False)


def fetch_options(ctx: Context, path: tuple[str, ...]) -> list[Option]:
    ctx.require_flake()
    if ctx.is_nixos:
        opts = "sys.options.home-manager.users.type.getSubOptions []"
        cfg = f'sys.config.home-manager.users."{ctx.user}"'
    else:
        opts, cfg = "sys.options", "sys.config"
    nix_path = "[ " + " ".join(json.dumps(p) for p in path) + " ]"
    expr = _OPTIONS_EXPR.replace("@OPTS@", opts).replace("@CFG@", cfg).replace("@PATH@", nix_path)
    raw = ctx.nix_eval_json(ctx.flake_system(), expr)
    return [
        Option(
            path=tuple(o["path"]), type=o["type"], kind=o["kind"],
            description=o["description"], values=o["values"],
            example=o["example"], current=o["current"],
        )
        for o in raw
    ]


def normalize_program(name: str) -> tuple[str, ...]:
    """``git`` → ``programs.git``; dotted names such as ``services.dunst`` are kept."""
    if "." in name:
        return split_option_path(name)
    return ("programs", name)


def parse_auto(text: str):
    """Interpret booleans, ``null`` and numbers; anything else is a string."""
    lowered = text.strip()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered == "null":
        return None
    if re.fullmatch(r"-?\d+", lowered):
        return int(lowered)
    if re.fullmatch(r"-?\d+\.\d+", lowered):
        return float(lowered)
    return text


def set_option(session: Session, path: tuple[str, ...], expr: str) -> None:
    """Stage ``path = expr`` in the file that already defines the closest ancestor."""
    nf = session.file_with_path(path) or session.load(session.ctx.home_file)
    session.save(nf.set_expr(path, expr))


def unset_option(session: Session, path: tuple[str, ...]) -> bool:
    """Stage removal of every definition of ``path``. Returns ``False`` if none exist."""
    removed = False
    for nf in session.modules():
        if nf.has_path(path):
            session.save(nf.unset(path))
            removed = True
    return removed


def _short(text: str, width: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def input_kind(option: Option) -> str:
    """How a value for ``option`` is entered.

    One of ``bool``, ``enum`` (pick from ``option.values``), ``freeform`` (sub-key plus
    value), ``list`` (comma-separated strings), ``string``, ``int`` or ``nix`` (raw
    expression).
    """
    if option.kind == "bool":
        return "bool"
    if option.values:
        return "enum"
    if option.is_freeform:
        return "freeform"
    if option.kind == "listOf" and "string" in option.type:
        return "list"
    if "string" in option.type or option.kind in ("str", "lines", "path"):
        return "string"
    if "integer" in option.type or "int" in option.kind.lower():
        return "int"
    return "nix"


def value_to_expr(option: Option, value, subkey: str = "") -> tuple[tuple[str, ...], str]:
    """Turn user input into ``(path, nix_expression)`` according to :func:`input_kind`.

    ``value`` is a Python value for ``bool``/``enum`` and text otherwise. Raises
    :class:`ValueError` for input that does not fit the option.
    """
    kind = input_kind(option)
    if kind in ("bool", "enum"):
        return option.path, to_nix(value)
    if kind == "freeform":
        if not subkey:
            raise ValueError("a sub-key is required")
        return (*option.path, *split_option_path(subkey)), to_nix(parse_auto(value))
    if kind == "list":
        return option.path, to_nix([v.strip() for v in value.split(",") if v.strip()])
    if kind == "string":
        return option.path, to_nix(value)
    if kind == "int":
        if not re.fullmatch(r"-?\d+", value.strip()):
            raise ValueError("expected an integer")
        return option.path, value.strip()
    return option.path, value


def prompt_value(option: Option) -> tuple[tuple[str, ...], str] | None:
    """Prompt on the terminal for a value suited to the option type.

    Returns ``(path, nix_expression)``, or ``None`` if the user cancels.
    """
    print()
    print(ui.bold(format_path(option.path)))
    if option.description:
        print(ui.dim(option.description.strip()[:600]))
    ui.info(f"Type: {option.type}   Current: {option.current_text}")
    if option.example:
        ui.info("Example: " + _short(option.example, 200))

    kind = input_kind(option)
    if kind == "bool":
        idx = ui.choose("Choose a value", ["on (true)", "off (false)"])
        return None if idx is None else value_to_expr(option, idx == 0)
    if kind == "enum":
        labels = [json.dumps(v, ensure_ascii=False) for v in option.values]
        idx = ui.choose("Choose a value", labels)
        return None if idx is None else value_to_expr(option, option.values[idx])
    if kind == "freeform":
        ui.info("Free-form setting. Enter a sub-key and a value (e.g. user.name → alice).")
        key = ui.ask("Sub-key (empty to cancel)")
        value = ui.ask("Value (true/false, number or string)") if key else ""
        return value_to_expr(option, value, key) if value else None

    prompts = {
        "list": "Comma-separated values (empty to cancel)",
        "string": "Value (empty to cancel)",
        "int": "Integer (empty to cancel)",
        "nix": "Raw Nix expression (empty to cancel)",
    }
    while True:
        value = ui.ask(prompts[kind])
        if not value:
            return None
        try:
            return value_to_expr(option, value)
        except ValueError as e:
            ui.warn(f"Invalid value: {e}.")


def configure(ctx: Context, name: str) -> Session | None:
    """Browse and edit a program's options. Returns a session holding the edits."""
    path = normalize_program(name)
    ui.info("Reading option metadata...")
    options = fetch_options(ctx, path)
    if not options:
        raise ui.EhmError(
            f"No options found under '{format_path(path)}'. Try `ehm programs {name}` to find the right name."
        )
    options.sort(key=lambda o: (o.path[-1] != "enable", o.is_freeform, o.kind == "package", o.path))
    session = Session(ctx)
    shown = options
    while True:
        print()
        print(ui.bold(f"Options for {format_path(path)}"))
        for i, opt in enumerate(shown, 1):
            rel = format_path(opt.path[len(path):])
            print(f"  {i:>3}) {rel:<28} {ui.dim(opt.current_text[:24]):<26} {_short(opt.description, 50)}")
        answer = ui.ask("Number to edit, search term, a = show all, q = done", "q")
        if answer.lower() == "q":
            break
        if answer.lower() == "a":
            shown = options
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(shown):
            result = prompt_value(shown[int(answer) - 1])
            if result:
                set_option(session, *result)
                ui.ok(f"{format_path(result[0])} = {result[1]}  (not saved yet)")
            continue
        lowered = answer.lower()
        matches = [o for o in options if lowered in format_path(o.path).lower() or lowered in o.description.lower()]
        if matches:
            shown = matches
        else:
            ui.warn("No matching options.")
    return session
