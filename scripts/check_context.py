#!/usr/bin/env python3
"""Check AGENTS.md path references and repo-map and command states.

The single-source-of-truth gate for the agent contract. Every repo-map / command
row in `AGENTS.md` carries a state mark (`exists` or `planned: <reason>`); this
linter fails the build when those marks drift from the real tree — a path marked
`exists` that is gone, or a `planned:` path that has since been created. Run last
by `scripts/check`.

Phase-1 tolerance: `AGENTS.md` is not authored until Phase 2, so a tree without
it is reported clean (exit 0) rather than as an error.
"""

import argparse
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

CODE_SPAN = re.compile(r"(?<!`)`([^`]+)`(?!`)")
LINK = re.compile(r"\]\((?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)")
PATH = re.compile(r"(?:[\w.+ -]+/)*[\w.+ -]+/?")
FILE = re.compile(r"[\w.+ -]*\.[A-Za-z][\w+-]*")
SEPARATOR = re.compile(r"\|[\s|:-]+\|")
STATE = re.compile(r"exists|planned: \S.*")


def code_paths(text, *, map_cell=False):
    """Find repo-root-relative code paths, including shell command operands."""
    for span in CODE_SPAN.findall(text):
        if map_cell:
            yield span
            continue
        try:
            words = shlex.split(span)
        except ValueError:
            continue
        for word in words:
            if word.startswith("-"):
                _, equals, word = word.partition("=")
                if not equals:
                    continue
            word = word.rstrip(";")
            if PATH.fullmatch(word) and ("/" in word or FILE.fullmatch(word)):
                yield word


def links(text):
    for angle, plain in LINK.findall(text):
        target = urlsplit(angle or plain)
        if target.path and not (target.scheme or target.netloc):
            yield unquote(target.path)


def present(root, path):
    candidate = root / path
    return candidate.resolve().is_relative_to(root) and candidate.exists()


def check(root):
    root = root.resolve()
    agents = root / "AGENTS.md"
    if not agents.exists():
        # Phase 1: AGENTS.md is authored in Phase 2. A tree without it is clean.
        return
    lines = agents.read_text(encoding="utf-8").splitlines()
    if len(lines) >= 200:
        yield "AGENTS.md must be under 200 lines"

    section = ""
    map_rows = 0
    for number, line in enumerate(lines, 1):
        if line.startswith("## "):
            section = line
        cells = line.strip().strip("|").split("|")
        is_table_row = line.startswith("|") and line.endswith("|")
        if is_table_row and section in ("## Repo map", "## Commands"):
            if cells[0].strip() in ("Path", "Action") or SEPARATOR.fullmatch(line):
                continue
            if len(cells) != 3 or not STATE.fullmatch(cells[2].strip()):
                yield f"AGENTS.md:{number}: use state 'exists' or 'planned: reason'"
                continue
            is_map = section == "## Repo map"
            reference, description = cells[:2] if is_map else cells[1::-1]
            marked = set(code_paths(reference, map_cell=is_map))
            if is_map:
                map_rows += 1
                if not marked:
                    yield f"AGENTS.md:{number}: repo-map path needs an inline code span"
            planned = cells[2].strip().startswith("planned:")
            if planned and not marked:
                yield f"AGENTS.md:{number}: planned row needs a literal path"
            for path in sorted(marked):
                exists = present(root, path)
                if exists and planned:
                    yield f"AGENTS.md:{number}: {path} marked planned but exists"
                elif not exists and not planned:
                    yield f"AGENTS.md:{number}: missing path {path}"
            line = description
        for path in sorted(set(code_paths(line)) | set(links(line))):
            if not present(root, path):
                yield f"AGENTS.md:{number}: missing path {path}"

    if not map_rows:
        yield "AGENTS.md needs a Repo map with at least one path row"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        findings = list(check(args.root))
    except (OSError, UnicodeError, ValueError) as error:
        findings = [str(error)]
    if findings:
        for finding in findings:
            print(f"context: FAIL: {finding}", file=sys.stderr)
        return 1
    print("context: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
