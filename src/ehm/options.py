"""Home Manager 옵션 정보를 읽고, 값을 대화형으로 고르게 한다."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import ui
from .context import Context
from .nixfile import format_path, split_option_path, to_nix
from .workspace import Session

# 옵션 트리를 걸어 다니며 이름, 타입, 설명, 현재 값을 모으는 Nix 함수.
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
        (if builtins.all scalar v then { value = v; } else { summary = "목록 ${toString (builtins.length v)}개"; })
      else if builtins.isAttrs v then
        (if (v.type or null) == "derivation" then { summary = "패키지 ${v.name or ""}"; }
         else if v == {} then { value = {}; }
         else { summary = "속성 ${toString (builtins.length (builtins.attrNames v))}개"; })
      else { summary = "함수/경로"; });
    in if t.success then t.value else { summary = "읽을 수 없음"; };
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
            return "(없음)"
        if value is True:
            return "켜짐"
        if value is False:
            return "꺼짐"
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
    """`git` → programs.git, `services.dunst` 는 그대로."""
    if "." in name:
        return split_option_path(name)
    return ("programs", name)


def parse_auto(text: str):
    """`true`, `12`, 그 외에는 문자열로 해석한다."""
    lowered = text.strip()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null",):
        return None
    if re.fullmatch(r"-?\d+", lowered):
        return int(lowered)
    if re.fullmatch(r"-?\d+\.\d+", lowered):
        return float(lowered)
    return text


def set_option(session: Session, path: tuple[str, ...], expr: str) -> None:
    nf = session.file_with_path(path) or session.load(session.ctx.home_file)
    session.save(nf.set_expr(path, expr))


def unset_option(session: Session, path: tuple[str, ...]) -> bool:
    removed = False
    for nf in session.modules():
        if nf.has_path(path):
            session.save(nf.unset(path))
            removed = True
    return removed


def _short(text: str, width: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def prompt_value(option: Option) -> tuple[tuple[str, ...], str] | None:
    """옵션 타입에 맞게 값을 묻는다. (경로, Nix 표현식) 또는 None(취소)."""
    print()
    print(ui.bold(format_path(option.path)))
    if option.description:
        print(ui.dim(option.description.strip()[:600]))
    ui.info(f"타입: {option.type}   현재 값: {option.current_text}")
    if option.example:
        ui.info("예시: " + _short(option.example, 200))

    if option.kind == "bool":
        idx = ui.choose("값을 고르세요", ["켜기 (true)", "끄기 (false)"])
        return None if idx is None else (option.path, "true" if idx == 0 else "false")

    if option.values:
        labels = [json.dumps(v, ensure_ascii=False) for v in option.values]
        idx = ui.choose("값을 고르세요", labels)
        return None if idx is None else (option.path, to_nix(option.values[idx]))

    if option.is_freeform:
        ui.info("자유 형식 설정입니다. 하위 키와 값을 입력하세요. (예: user.name → jihoo)")
        key = ui.ask("하위 키 (취소: 빈 값)")
        if not key:
            return None
        sub = split_option_path(key)
        value = ui.ask("값 (true/숫자/문자열)")
        if not value:
            return None
        return (*option.path, *sub), to_nix(parse_auto(value))

    if option.kind == "listOf" and "string" in option.type:
        value = ui.ask("값들을 쉼표로 구분해 입력 (취소: 빈 값)")
        if not value:
            return None
        return option.path, to_nix([v.strip() for v in value.split(",") if v.strip()])

    if "string" in option.type or option.kind in ("str", "lines", "path"):
        value = ui.ask("값 (취소: 빈 값)")
        return (option.path, to_nix(value)) if value else None

    if "integer" in option.type or "int" in option.kind.lower():
        while True:
            value = ui.ask("숫자 (취소: 빈 값)")
            if not value:
                return None
            if re.fullmatch(r"-?\d+", value):
                return option.path, value
            ui.warn("숫자를 입력해 주세요.")

    value = ui.ask("Nix 표현식을 그대로 입력 (취소: 빈 값)")
    return (option.path, value) if value else None


def configure(ctx: Context, name: str) -> Session | None:
    """프로그램 옵션을 보여주고 고르게 한다. 바꾼 내용이 담긴 세션을 돌려준다."""
    path = normalize_program(name)
    ui.info("옵션 정보를 읽는 중...")
    options = fetch_options(ctx, path)
    if not options:
        raise ui.EhmError(
            f"'{format_path(path)}' 옵션을 찾지 못했습니다. `ehm programs {name}` 으로 이름을 찾아보세요."
        )
    options.sort(key=lambda o: (o.path[-1] != "enable", o.is_freeform, o.kind == "package", o.path))
    session = Session(ctx)
    shown = options
    while True:
        print()
        print(ui.bold(f"{format_path(path)} 옵션"))
        for i, opt in enumerate(shown, 1):
            rel = format_path(opt.path[len(path):])
            print(f"  {i:>3}) {rel:<28} {ui.dim(opt.current_text[:24]):<26} {_short(opt.description, 50)}")
        answer = ui.ask("바꿀 번호, 검색어, 전체 목록: a, 끝내기: q", "q")
        if answer.lower() == "q":
            break
        if answer.lower() == "a":
            shown = options
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(shown):
            result = prompt_value(shown[int(answer) - 1])
            if result:
                set_option(session, *result)
                ui.ok(f"{format_path(result[0])} = {result[1]}  (아직 저장 전)")
            continue
        lowered = answer.lower()
        matches = [o for o in options if lowered in format_path(o.path).lower() or lowered in o.description.lower()]
        if matches:
            shown = matches
        else:
            ui.warn("찾지 못했습니다.")
    return session
