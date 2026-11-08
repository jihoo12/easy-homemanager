"""Terminal I/O helpers. Standard library only."""

from __future__ import annotations

import difflib
import os
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


class EhmError(Exception):
    """An error whose message is shown to the user as-is."""


#: When true, confirmations are answered "yes" without prompting (set by ``-y``).
ASSUME_YES = False

_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# ANSI SGR codes used across the CLI.
RED, GREEN, YELLOW, MAGENTA, CYAN, DIM = "31", "32", "33", "35", "36", "2"

#: Receives ``(kind, message)`` instead of the terminal while a full-screen UI runs.
Sink = Callable[[str, str], None]
_sink: Sink | None = None


@contextmanager
def redirect(sink: Sink | None) -> Iterator[None]:
    """Route step/ok/info/warn/error messages to ``sink`` (``None`` restores the terminal).

    While a sink is installed, interactive prompts are refused, because they would
    block on a terminal that the full-screen UI owns.
    """
    global _sink
    previous, _sink = _sink, sink
    try:
        yield
    finally:
        _sink = previous


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _require_terminal() -> None:
    if _sink is not None:
        raise RuntimeError("interactive prompt requested while output is redirected")


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(text: str) -> str:
    return paint("1", text)


def dim(text: str) -> str:
    return paint(DIM, text)


def step(msg: str) -> None:
    if _sink is not None:
        _sink("step", strip_ansi(msg))
        return
    print(f"\n{paint('1;36', '▶')} {bold(msg)}", flush=True)


def ok(msg: str) -> None:
    if _sink is not None:
        _sink("ok", strip_ansi(msg))
        return
    print(f"{paint(GREEN, '✔')} {msg}", flush=True)


def info(msg: str) -> None:
    if _sink is not None:
        _sink("info", strip_ansi(msg))
        return
    print(f"  {msg}", flush=True)


def warn(msg: str) -> None:
    if _sink is not None:
        _sink("warn", strip_ansi(msg))
        return
    print(f"{paint(YELLOW, '!')} {msg}", flush=True)


def error(msg: str) -> None:
    if _sink is not None:
        _sink("error", strip_ansi(msg))
        return
    print(f"{paint(RED, '✘')} {msg}", file=sys.stderr, flush=True)


def ask(prompt: str, default: str = "") -> str:
    """Read one line. Returns ``default`` on empty input or EOF."""
    _require_terminal()
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        print()
        return default
    return answer or default


def confirm(question: str, default: bool = False) -> bool:
    if ASSUME_YES:
        return True
    _require_terminal()
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{hint}] ").strip().lower()
    except EOFError:
        print()
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def choose(title: str, options: list[str], allow_cancel: bool = True) -> int | None:
    """Pick one option by number. Returns ``None`` if cancelled."""
    print(bold(title))
    for i, opt in enumerate(options, 1):
        print(f"  {i:>2}) {opt}")
    while True:
        answer = ask("Number" + (" (q to cancel)" if allow_cancel else ""))
        if answer.lower() in ("q", "") and allow_cancel:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        warn("Please enter a number from the list.")


def parse_selection(answer: str, count: int) -> list[int] | None:
    """Parse ``1 3 5``, ``1-3`` or ``a`` (all) into zero-based indices.

    Returns ``None`` if the input is malformed or selects nothing.
    """
    answer = answer.strip().lower()
    if answer == "a":
        return list(range(count))
    chosen: set[int] = set()
    for part in answer.replace(",", " ").split():
        lo, sep, hi = part.partition("-")
        if sep and lo.isdigit() and hi.isdigit() and 1 <= int(lo) <= int(hi) <= count:
            chosen.update(range(int(lo) - 1, int(hi)))
        elif not sep and part.isdigit() and 1 <= int(part) <= count:
            chosen.add(int(part) - 1)
        else:
            return None
    return sorted(chosen) or None


def choose_many(prompt: str, count: int) -> list[int]:
    """Pick several options. Returns an empty list if cancelled."""
    while True:
        answer = ask(f"{prompt} (e.g. 1 3, 2-4; a = all, q = cancel)", "q")
        if answer.lower() == "q":
            return []
        selection = parse_selection(answer, count)
        if selection is not None:
            return selection
        warn("Could not understand that selection. Please try again.")


def unified_diff(path: Path, old: str, new: str, labels: tuple[str, str] = ("current", "new")) -> list[str]:
    return list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), f"{path} ({labels[0]})", f"{path} ({labels[1]})", lineterm="", n=2
    ))


def show_diff(path: Path, old: str, new: str) -> None:
    for line in unified_diff(path, old, new):
        if line.startswith(("---", "+++")):
            print(bold(line))
        elif line.startswith("@@"):
            print(paint(CYAN, line))
        elif line.startswith("+"):
            print(paint(GREEN, line))
        elif line.startswith("-"):
            print(paint(RED, line))
        else:
            print(line)


def tilde(path: Path | str) -> str:
    """Abbreviate the home directory as ``~`` for display."""
    text = str(path)
    home = str(Path.home())
    if text == home or text.startswith(home + "/"):
        return "~" + text[len(home) :]
    return text
