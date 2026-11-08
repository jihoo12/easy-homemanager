"""A small, format-preserving reader/editor for Nix module files.

This is not a full Nix parser. The source is tokenized, only the
``attr.path = value;`` structure of the module body is recovered, and every edit
replaces just the affected span of the original text, so comments and layout
survive. Values that cannot be expanded statically (``lib.mkIf cond { ... }``,
``lib.mkMerge [ ... ]``) are treated as opaque and never rewritten.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class NixParseError(Exception):
    """The source cannot be tokenized or its brackets are unbalanced."""


class ModuleShapeError(Exception):
    """The source is valid but not in a shape that can be edited automatically."""


@dataclass(frozen=True)
class Token:
    kind: str  # ident | str | path | num | punct | interp
    text: str
    start: int
    end: int


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_'\-]*")
_NUMBER = re.compile(r"\d+(?:\.\d*)?(?:[eE][+-]?\d+)?")
_PATH = re.compile(r"(?:~|\.{1,2}|[A-Za-z0-9_.+\-]+)?(?:/[A-Za-z0-9_.+\-]+)+/?")
_PATH_TAIL = re.compile(r"[A-Za-z0-9_.+\-/]*")
_URI = re.compile(r"[A-Za-z][A-Za-z0-9+\-.]*:[A-Za-z0-9%/?:@&=+$,\-_.!~*']+")
_OPS = ("...", "++", "//", "==", "!=", "<=", ">=", "&&", "||", "->")


def _scan_dq(src: str, i: int) -> int:
    i += 1
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
        elif src.startswith("$$", i):
            i += 2
        elif src.startswith("${", i):
            i = _scan_interp(src, i)
        elif c == '"':
            return i + 1
        else:
            i += 1
    raise NixParseError("unterminated string")


def _scan_ind(src: str, i: int) -> int:
    i += 2
    n = len(src)
    while i < n:
        if src.startswith("''", i):
            nxt = src[i + 2 : i + 3]
            if nxt in ("'", "$"):
                i += 3
            elif nxt == "\\":
                i += 4
            else:
                return i + 2
        elif src.startswith("$$", i):
            i += 2
        elif src.startswith("${", i):
            i = _scan_interp(src, i)
        else:
            i += 1
    raise NixParseError("unterminated '' string")


def _scan_interp(src: str, i: int) -> int:
    i += 2
    depth = 1
    n = len(src)
    while i < n:
        c = src[i]
        if c == "#":
            nl = src.find("\n", i)
            i = n if nl < 0 else nl + 1
        elif src.startswith("/*", i):
            e = src.find("*/", i + 2)
            if e < 0:
                raise NixParseError("unterminated comment")
            i = e + 2
        elif c == '"':
            i = _scan_dq(src, i)
        elif src.startswith("''", i):
            i = _scan_ind(src, i)
        elif c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        else:
            i += 1
    raise NixParseError("unterminated ${ ... }")


def tokenize(src: str) -> list[Token]:
    """Return all tokens, excluding whitespace and comments."""
    tokens: list[Token] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c == "#":
            nl = src.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if src.startswith("/*", i):
            e = src.find("*/", i + 2)
            if e < 0:
                raise NixParseError("unterminated comment")
            i = e + 2
            continue
        if c == '"':
            e = _scan_dq(src, i)
            tokens.append(Token("str", src[i:e], i, e))
            i = e
            continue
        if src.startswith("''", i):
            e = _scan_ind(src, i)
            tokens.append(Token("str", src[i:e], i, e))
            i = e
            continue
        if src.startswith("${", i):
            e = _scan_interp(src, i)
            tokens.append(Token("interp", src[i:e], i, e))
            i = e
            continue
        m = _URI.match(src, i)
        if m:
            tokens.append(Token("path", m.group(), i, m.end()))
            i = m.end()
            continue
        m = _PATH.match(src, i)
        if m:
            e = m.end()
            while src.startswith("${", e):  # ./foo/${bar}/baz
                e = _scan_interp(src, e)
                e = _PATH_TAIL.match(src, e).end()
            tokens.append(Token("path", src[i:e], i, e))
            i = e
            continue
        m = _NUMBER.match(src, i)
        if m:
            tokens.append(Token("num", m.group(), i, m.end()))
            i = m.end()
            continue
        m = _IDENT.match(src, i)
        if m:
            tokens.append(Token("ident", m.group(), i, m.end()))
            i = m.end()
            continue
        for op in _OPS:
            if src.startswith(op, i):
                tokens.append(Token("punct", op, i, i + len(op)))
                i += len(op)
                break
        else:
            tokens.append(Token("punct", c, i, i + 1))
            i += 1
    return tokens


def _match_brackets(tokens: list[Token]) -> dict[int, int]:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[int] = []
    out: dict[int, int] = {}
    for idx, tok in enumerate(tokens):
        if tok.kind != "punct":
            continue
        if tok.text in "([{":
            stack.append(idx)
        elif tok.text in pairs:
            if not stack or tokens[stack[-1]].text != pairs[tok.text]:
                raise NixParseError(f"unbalanced bracket at offset {tok.start}")
            o = stack.pop()
            out[o] = idx
            out[idx] = o
    if stack:
        raise NixParseError("unclosed bracket")
    return out


_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_'\-]*$")
_KEYWORDS = {"if", "then", "else", "assert", "with", "let", "in", "rec", "inherit", "or"}
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_.+\-/]+$")


def nix_string(value: str) -> str:
    """Render a Python string as a Nix string literal."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("${", "\\${")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def format_key(key: str) -> str:
    if _SAFE_KEY.match(key) and key not in _KEYWORDS:
        return key
    return nix_string(key)


def format_path(path: tuple[str, ...] | list[str]) -> str:
    return ".".join(format_key(p) for p in path)


def split_option_path(text: str) -> tuple[str, ...]:
    """Split an option path such as ``programs.git."a.b".enable`` into its parts."""
    tokens = tokenize(text)
    parts: list[str] = []
    expect_part = True
    for t in tokens:
        if expect_part:
            if t.kind == "ident":
                parts.append(t.text)
            elif t.kind == "str" and t.text.startswith('"'):
                parts.append(_unquote(t.text))
            elif t.kind == "num":
                parts.append(t.text)
            else:
                raise ModuleShapeError(f"invalid option path: {text}")
            expect_part = False
        else:
            if t.text != ".":
                raise ModuleShapeError(f"invalid option path: {text}")
            expect_part = True
    if expect_part or not parts:
        raise ModuleShapeError(f"invalid option path: {text}")
    return tuple(parts)


def _unquote(raw: str) -> str:
    if raw.startswith('"') and "${" not in raw:
        return (
            raw[1:-1]
            .replace('\\"', '"')
            .replace("\\$", "$")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )
    return raw


def to_nix(value, indent: str = "") -> str:
    """Render a Python value as a Nix expression."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return nix_string(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "[ ]"
        return "[ " + " ".join(to_nix(v, indent) for v in value) + " ]"
    if isinstance(value, dict):
        if not value:
            return "{ }"
        inner = indent + "  "
        lines = [f"{inner}{format_key(k)} = {to_nix(v, inner)};" for k, v in value.items()]
        return "{\n" + "\n".join(lines) + f"\n{indent}}}"
    raise TypeError(f"cannot convert to Nix: {value!r}")


def relative_nix_path(target: Path, base_dir: Path) -> str:
    """A path expression pointing at ``target`` from a Nix file in ``base_dir``."""
    rel = os.path.relpath(target, base_dir)
    if not rel.startswith("."):
        rel = "./" + rel
    elif rel.startswith(".") and not rel.startswith(("./", "../")):
        rel = "./" + rel  # dot-prefixed names such as .config/foo
    if _SAFE_PATH.match(rel):
        return rel
    # Characters such as spaces are not allowed in path literals; concatenate a string.
    if rel.startswith("../"):
        ups, _, rest = rel.rpartition("../")
        return f"({ups}../. + {nix_string('/' + rest)})"
    return f"(./. + {nix_string(rel[1:])})"


@dataclass
class Binding:
    path: tuple[str, ...]
    start: int  # offset of the first key character
    value_start: int
    value_end: int
    end: int  # offset just past the ';'
    value_tok: int
    end_tok: int  # index of the ';' token
    depth: int  # 0 for top-level statements of the module
    attrs_open: int | None = None  # index of '{' when the value is an attribute set
    attrs_close: int | None = None


@dataclass
class ListItem:
    name: str
    start: int
    end: int
    simple: bool  # a plain name, as opposed to e.g. (python3.withPackages ...)


@dataclass
class ListInfo:
    open: int
    close: int
    with_pkgs: bool
    items: list[ListItem] = field(default_factory=list)


class NixFile:
    def __init__(self, text: str, path: Path | None = None):
        self.text = text
        self.path = path
        self.tokens = tokenize(text)
        self.match = _match_brackets(self.tokens)
        self.pattern_open: int | None = None
        self.root_open: int | None = None
        self.root_close: int | None = None
        self.bindings: list[Binding] = []
        self.shape_error: str | None = None
        try:
            self._find_root()
            self._scan_frame(self.root_open, self.root_close, (), 0)
        except ModuleShapeError as e:
            self.shape_error = str(e)

    # ---- structure -----------------------------------------------------
    def _tok(self, i: int) -> Token | None:
        return self.tokens[i] if 0 <= i < len(self.tokens) else None

    def _is(self, i: int, text: str) -> bool:
        t = self._tok(i)
        return t is not None and t.kind in ("punct", "ident") and t.text == text

    def _skip_let(self, i: int) -> int:
        """Return the index of the ``in`` matching the ``let`` at ``i``."""
        depth = 1
        j = i + 1
        while j < len(self.tokens):
            t = self.tokens[j]
            if t.kind == "punct" and t.text in ("(", "[", "{"):
                j = self.match[j] + 1
                continue
            if t.kind == "ident" and t.text == "let":
                depth += 1
            elif t.kind == "ident" and t.text == "in":
                depth -= 1
                if depth == 0:
                    return j
            j += 1
        raise ModuleShapeError("no matching `in` for `let`")

    def _stmt_end(self, i: int, limit: int) -> int:
        """Return the index of the ';' that ends the statement whose value starts at ``i``."""
        lets = 0
        pending = 0  # ';' still owed by enclosing with/assert
        j = i
        while j < limit:
            t = self.tokens[j]
            if t.kind == "punct" and t.text in ("(", "[", "{"):
                j = self.match[j] + 1
                continue
            if t.kind == "ident":
                if t.text == "let":
                    lets += 1
                elif t.text == "in" and lets:
                    lets -= 1
                elif t.text in ("with", "assert"):
                    pending += 1
            elif t.kind == "punct" and t.text == ";":
                if pending:
                    pending -= 1
                elif not lets:
                    return j
            j += 1
        raise ModuleShapeError("missing ';' at end of statement")

    def _find_root(self) -> None:
        i = 0
        for _ in range(64):
            t = self._tok(i)
            if t is None:
                raise ModuleShapeError("file is empty")
            if t.text == "{" and t.kind == "punct":
                close = self.match[i]
                if self._is(close + 1, ":"):
                    self.pattern_open = i
                    i = close + 2
                    continue
                if self._is(close + 1, "@") and self._is(close + 3, ":"):
                    self.pattern_open = i
                    i = close + 4
                    continue
                self.root_open, self.root_close = i, close
                return
            if t.kind == "ident":
                if self._is(i + 1, ":"):
                    i += 2
                    continue
                if self._is(i + 1, "@") and self._is(i + 2, "{"):
                    self.pattern_open = i + 2
                    i = self.match[i + 2] + 2
                    continue
                if t.text == "let":
                    i = self._skip_let(i) + 1
                    continue
                if t.text in ("with", "assert"):
                    i = self._stmt_end_simple(i) + 1
                    continue
                if t.text == "rec" and self._is(i + 1, "{"):
                    self.root_open, self.root_close = i + 1, self.match[i + 1]
                    return
            raise ModuleShapeError(
                f"module body is not a plain attribute set (near '{t.text}'); edit it manually"
            )
        raise ModuleShapeError("could not locate the module body")

    def _stmt_end_simple(self, i: int) -> int:
        j = i + 1
        while j < len(self.tokens):
            t = self.tokens[j]
            if t.kind == "punct" and t.text in ("(", "[", "{"):
                j = self.match[j] + 1
                continue
            if t.kind == "punct" and t.text == ";":
                return j
            j += 1
        raise ModuleShapeError("missing ';'")

    def _parse_attrpath(self, i: int, limit: int) -> tuple[tuple[str, ...], int] | None:
        parts: list[str] = []
        j = i
        while j < limit:
            t = self.tokens[j]
            if t.kind == "ident":
                parts.append(t.text)
            elif t.kind == "str":
                parts.append(_unquote(t.text))
            elif t.kind in ("interp", "num"):
                parts.append(t.text)
            else:
                return None
            j += 1
            if self._is(j, "."):
                j += 1
                continue
            if self._is(j, "="):
                return tuple(parts), j
            return None
        return None

    def _scan_frame(self, open_idx: int, close_idx: int, prefix: tuple[str, ...], depth: int) -> None:
        i = open_idx + 1
        while i < close_idx:
            t = self.tokens[i]
            if t.kind == "ident" and t.text == "inherit":
                i = self._stmt_end_simple(i) + 1
                continue
            parsed = self._parse_attrpath(i, close_idx)
            if parsed is None:
                raise ModuleShapeError(f"could not parse attribute definition (near '{t.text}')")
            path, eq = parsed
            v = eq + 1
            end = self._stmt_end(v, close_idx)
            first = self.tokens[v]
            last = self.tokens[end - 1]
            b = Binding(
                path=prefix + path,
                start=t.start,
                value_start=first.start,
                value_end=last.end,
                end=self.tokens[end].end,
                value_tok=v,
                end_tok=end,
                depth=depth,
            )
            ov = v + 1 if self._is(v, "rec") else v
            if self._is(ov, "{") and self.match[ov] == end - 1:
                b.attrs_open, b.attrs_close = ov, end - 1
            self.bindings.append(b)
            if b.attrs_open is not None:
                self._scan_frame(b.attrs_open, b.attrs_close, b.path, depth + 1)
            i = end + 1

    def ensure_editable(self) -> None:
        if self.shape_error:
            where = f"{self.path}: " if self.path else ""
            raise ModuleShapeError(where + self.shape_error)

    # ---- queries -------------------------------------------------------
    def find(self, path: tuple[str, ...]) -> Binding | None:
        for b in self.bindings:
            if b.path == path:
                return b
        return None

    def value_text(self, b: Binding) -> str:
        return self.text[b.value_start : b.value_end]

    def has_path(self, path: tuple[str, ...]) -> bool:
        """Whether any binding sets ``path`` or something below it."""
        n = len(path)
        return any(b.path[:n] == path for b in self.bindings)

    def list_info(self, b: Binding) -> list[ListInfo]:
        """Find the list literals in a value, including ``with pkgs; [ ... ] ++ [ ... ]``."""
        out: list[ListInfo] = []
        with_pkgs = False
        i = b.value_tok
        while i < b.end_tok:
            t = self.tokens[i]
            if t.kind == "ident" and t.text == "with":
                if self._is(i + 1, "pkgs") and self._is(i + 2, ";"):
                    with_pkgs = True
            if t.kind == "punct" and t.text == "[":
                close = self.match[i]
                out.append(
                    ListInfo(
                        open=t.start,
                        close=self.tokens[close].start,
                        with_pkgs=with_pkgs,
                        items=self._list_items(i, close),
                    )
                )
                i = close + 1
                continue
            if t.kind == "punct" and t.text == "{":
                i = self.match[i] + 1
                continue
            i += 1
        return out

    def _list_items(self, open_idx: int, close_idx: int) -> list[ListItem]:
        items: list[ListItem] = []
        j = open_idx + 1
        while j < close_idx:
            t = self.tokens[j]
            start = t.start
            simple = t.kind == "ident"
            if t.kind == "punct" and t.text in ("(", "[", "{"):
                j = self.match[j]
                simple = False
            j += 1
            while self._is(j, ".") and j + 1 < close_idx:
                j += 2
            end = self.tokens[j - 1].end
            items.append(ListItem(self.text[start:end], start, end, simple))
        return items

    # ---- edits ---------------------------------------------------------
    def _replaced(self, start: int, end: int, new: str) -> NixFile:
        return NixFile(self.text[:start] + new + self.text[end:], self.path)

    def _line_start(self, offset: int) -> int:
        return self.text.rfind("\n", 0, offset) + 1

    def _line_indent(self, offset: int) -> str:
        ls = self._line_start(offset)
        m = re.match(r"[ \t]*", self.text[ls:])
        return m.group() if m else ""

    def _first_on_line(self, offset: int) -> bool:
        return not self.text[self._line_start(offset) : offset].strip()

    def _line_end_after(self, offset: int) -> int | None:
        """End of the line after ``offset``, or ``None`` if anything but a comment follows."""
        nl = self.text.find("\n", offset)
        nl = len(self.text) if nl < 0 else nl
        rest = self.text[offset:nl].strip()
        if rest and not rest.startswith("#"):
            return None
        return nl

    def _remove_span(self, start: int, end: int) -> tuple[int, int]:
        """Span to delete; the whole line if the element stands alone on it."""
        line_end = self._line_end_after(end)
        if self._first_on_line(start) and line_end is not None:
            s = self._line_start(start)
            return s, min(line_end + 1, len(self.text))
        e = end
        while e < len(self.text) and self.text[e] in " \t":
            e += 1
        return start, e

    def add_list_item(self, path: tuple[str, ...], name: str, raw: bool = False, packages: bool = True) -> NixFile:
        """Append ``name`` to the list at ``path``, creating the definition if needed.

        A plain package name gets a ``pkgs.`` prefix unless the list is inside
        ``with pkgs;``; with ``raw`` the text is inserted unchanged. A new list is
        written as ``with pkgs; [ ... ]`` unless ``packages`` is false (e.g. ``imports``).
        """
        self.ensure_editable()
        b = self.find(path)
        if b is None:
            if not packages:
                return self._insert_statement(path, f"[\n{{INDENT}}  {name}\n{{INDENT}}]")
            nf = self.ensure_arg("pkgs")
            expr = f"with pkgs; [\n{{INDENT}}  {name}\n{{INDENT}}]"
            return nf._insert_statement(path, expr)
        lists = self.list_info(b)
        if not lists:
            raise ModuleShapeError(
                f"no list literal found in {format_path(path)}; edit it manually"
            )
        lst = lists[0]
        text = name if raw or lst.with_pkgs else f"pkgs.{name}"
        close = lst.close
        if self._first_on_line(close):
            first_items = [it for it in lst.items if self._first_on_line(it.start)]
            if first_items:
                indent = self._line_indent(first_items[-1].start)
            else:
                indent = self._line_indent(close) + "  "
            ls = self._line_start(close)
            return self._replaced(ls, ls, f"{indent}{text}\n")
        # Single-line lists such as `[ a b ]`.
        before = self.text[lst.open + 1 : close]
        if before.strip():
            trimmed = before.rstrip()
            return self._replaced(lst.open + 1, close, f"{trimmed} {text} ")
        return self._replaced(lst.open + 1, close, f" {text} ")

    def remove_list_item(self, path: tuple[str, ...], name: str) -> NixFile:
        self.ensure_editable()
        b = self.find(path)
        if b is None:
            raise KeyError(name)
        for lst in self.list_info(b):
            for it in lst.items:
                if it.simple and normalize_pkg(it.name) == normalize_pkg(name):
                    s, e = self._remove_span(it.start, it.end)
                    return self._replaced(s, e, "")
        raise KeyError(name)

    def arg_names(self) -> list[str]:
        """Names in the module's ``{ a, b, ... }:`` argument pattern (empty if there is none)."""
        if self.pattern_open is None:
            return []
        close = self.match[self.pattern_open]
        names = []
        i = self.pattern_open + 1
        while i < close:
            t = self.tokens[i]
            if t.kind == "punct" and t.text in ("(", "[", "{"):
                i = self.match[i] + 1
                continue
            if t.kind == "ident" and (self._is(i - 1, "{") or self._is(i - 1, ",")):
                names.append(t.text)
            i += 1
        return names

    def ensure_arg(self, name: str) -> NixFile:
        """Add ``name`` (``pkgs``, ``config``, ...) to the module's argument pattern if missing."""
        self.ensure_editable()
        if self.pattern_open is None:
            # Not a function, or an `args:` lambda: we cannot tell whether `name` is in scope.
            first = self.tokens[0] if self.tokens else None
            if first is not None and first.kind == "punct" and first.text == "{":
                new = "{ " + name + ", ... }:\n"
                return NixFile(new + self.text, self.path)
            raise ModuleShapeError(f"cannot add '{name}' to the module arguments automatically")
        close = self.match[self.pattern_open]
        if name in self.arg_names():
            return self
        at = self.tokens[self.pattern_open].end
        sep = " " if self.tokens[self.pattern_open + 1].start > at else ""
        new = f" {name}," if sep else f"{name}, "
        if close == self.pattern_open + 1:  # `{ }:`
            new = f" {name} "
        return self._replaced(at, at, new)

    def _render_statement(self, rel: tuple[str, ...], expr: str, indent: str) -> str:
        return f"{indent}{format_path(rel)} = {expr.replace('{INDENT}', indent)};"

    def _insert_in_frame(self, open_idx: int, close_idx: int, rel: tuple[str, ...], expr: str, depth: int) -> NixFile:
        close_tok = self.tokens[close_idx]
        children = [
            b for b in self.bindings
            if b.depth == depth
            and self.tokens[open_idx].end <= b.start
            and b.end <= close_tok.start
        ]
        if self._first_on_line(close_tok.start):
            if children and self._first_on_line(children[0].start):
                indent = self._line_indent(children[0].start)
            else:
                indent = self._line_indent(close_tok.start) + "  "
            ls = self._line_start(close_tok.start)
            stmt = self._render_statement(rel, expr, indent)
            return self._replaced(ls, ls, stmt + "\n")
        stmt = self._render_statement(rel, expr, "")
        before = self.text[self.tokens[open_idx].end : close_tok.start]
        return self._replaced(
            self.tokens[open_idx].end, close_tok.start,
            f"{before.rstrip()} {stmt} " if before.strip() else f" {stmt} ",
        )

    def _insert_statement(self, path: tuple[str, ...], expr: str) -> NixFile:
        """Insert a top-level statement, next to related definitions when there are any."""
        self.ensure_editable()
        siblings = [
            b for b in self.bindings
            if b.depth == 0 and len(path) >= 2 and b.path[:2] == path[:2]
        ] or [
            b for b in self.bindings
            if b.depth == 0 and len(path) >= 2 and len(b.path) >= 2 and b.path[0] == path[0]
        ]
        if siblings:
            last = siblings[-1]
            nl = self._line_end_after(last.end)
            if nl is not None and self._first_on_line(last.start):
                indent = self._line_indent(last.start)
                stmt = self._render_statement(path, expr, indent)
                return self._replaced(nl, nl, "\n" + stmt)
        return self._insert_in_frame(self.root_open, self.root_close, path, expr, 0)

    def set_expr(self, path: tuple[str, ...], expr: str) -> NixFile:
        """Set ``path`` to the Nix expression ``expr``, replacing or inserting as needed."""
        self.ensure_editable()
        b = self.find(path)
        if b is not None:
            indent = self._line_indent(b.start)
            return self._replaced(b.value_start, b.value_end, expr.replace("{INDENT}", indent))
        for n in range(len(path) - 1, 0, -1):
            parent = self.find(path[:n])
            if parent is None:
                continue
            if parent.attrs_open is None:
                raise ModuleShapeError(
                    f"{format_path(path[:n])} is not a plain attribute set and cannot be edited automatically"
                )
            return self._insert_in_frame(
                parent.attrs_open, parent.attrs_close, path[n:], expr, parent.depth + 1
            )
        return self._insert_statement(path, expr)

    def unset(self, path: tuple[str, ...]) -> NixFile:
        """Remove ``path`` and everything below it. Raises ``KeyError`` if absent."""
        self.ensure_editable()
        n = len(path)
        matches = [b for b in self.bindings if b.path[:n] == path]
        if not matches:
            raise KeyError(format_path(path))
        spans: list[tuple[int, int]] = []
        for b in sorted(matches, key=lambda x: x.start):
            if spans and b.start < spans[-1][1]:
                continue
            spans.append(self._remove_span(b.start, b.end))
        text = self.text
        for s, e in reversed(spans):
            text = text[:s] + text[e:]
        return NixFile(text, self.path)

    def import_paths(self) -> list[str]:
        """Relative paths listed in ``imports = [ ./a.nix ./b ];``."""
        b = self.find(("imports",))
        if b is None:
            return []
        out = []
        for lst in self.list_info(b):
            for it in lst.items:
                if it.name.startswith(("./", "../")):
                    out.append(it.name)
        return out


def normalize_pkg(name: str) -> str:
    """Strip a leading ``pkgs.`` from a package reference."""
    return name[5:] if name.startswith("pkgs.") else name
