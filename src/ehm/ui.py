"""터미널 입출력 도우미. 외부 의존성 없이 동작한다."""

from __future__ import annotations

import difflib
import os
import sys
from pathlib import Path


class EhmError(Exception):
    """사용자에게 그대로 보여줄 오류."""


ASSUME_YES = False
_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(text: str) -> str:
    return paint("1", text)


def dim(text: str) -> str:
    return paint("2", text)


def step(msg: str) -> None:
    print(f"\n{paint('1;36', '▶')} {bold(msg)}", flush=True)


def ok(msg: str) -> None:
    print(f"{paint('32', '✔')} {msg}", flush=True)


def info(msg: str) -> None:
    print(f"  {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"{paint('33', '!')} {msg}", flush=True)


def error(msg: str) -> None:
    print(f"{paint('31', '✘')} {msg}", file=sys.stderr, flush=True)


def ask(prompt: str, default: str = "") -> str:
    """한 줄 입력. 입력이 끝났으면(EOF) 기본값을 돌려준다."""
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
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{hint}] ").strip().lower()
    except EOFError:
        print()
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "ㅛ", "네", "예", "응")


def choose(title: str, options: list[str], allow_cancel: bool = True) -> int | None:
    """번호로 하나를 고른다. 취소하면 None."""
    print(bold(title))
    for i, opt in enumerate(options, 1):
        print(f"  {i:>2}) {opt}")
    while True:
        answer = ask("번호" + (" (취소: q)" if allow_cancel else ""))
        if answer.lower() in ("q", "") and allow_cancel:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        warn("목록에 있는 번호를 입력해 주세요.")


def choose_many(prompt: str, count: int) -> list[int]:
    """`1 3 5`, `1-3`, `a`(전부), `q`(취소) 형식으로 여러 개를 고른다."""
    while True:
        answer = ask(f"{prompt} (예: 1 3, 2-4, 전부: a, 취소: q)", "q").lower()
        if answer == "q":
            return []
        if answer == "a":
            return list(range(count))
        chosen: set[int] = set()
        valid = True
        for part in answer.replace(",", " ").split():
            if "-" in part:
                lo, _, hi = part.partition("-")
                if lo.isdigit() and hi.isdigit() and 1 <= int(lo) <= int(hi) <= count:
                    chosen.update(range(int(lo) - 1, int(hi)))
                else:
                    valid = False
            elif part.isdigit() and 1 <= int(part) <= count:
                chosen.add(int(part) - 1)
            else:
                valid = False
        if valid and chosen:
            return sorted(chosen)
        warn("입력을 이해하지 못했습니다. 다시 입력해 주세요.")


def show_diff(path: Path, old: str, new: str) -> None:
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(), f"{path} (현재)", f"{path} (변경 후)", lineterm="", n=2
    )
    for line in lines:
        if line.startswith(("---", "+++")):
            print(bold(line))
        elif line.startswith("@@"):
            print(paint("36", line))
        elif line.startswith("+"):
            print(paint("32", line))
        elif line.startswith("-"):
            print(paint("31", line))
        else:
            print(line)


def tilde(path: Path | str) -> str:
    text = str(path)
    home = str(Path.home())
    if text == home or text.startswith(home + "/"):
        return "~" + text[len(home) :]
    return text
