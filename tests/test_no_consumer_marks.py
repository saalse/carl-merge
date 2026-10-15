"""Locked: carl-merge names none of the projects or people that use it.

A consumer's tracker cites carl-merge, never the other way round. The marks
are an issue reference (`#` and three or four digits), a decision id (`D` and
two digits), the first consumer's name in any spelling, its owner's names, a
money figure, and any URL host outside GitHub and the reserved example
domains.

Every tracked text file is read, except `uv.lock`, which holds only hashes and
versions, this file, which spells the marks out, and the PNG logo, which is an
image and holds no text.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MARKS = (
    re.compile(r"#\d{3,4}\b"),
    re.compile(r"\bD\d{2}\b"),
    re.compile(r"the[ _-]?numbers", re.IGNORECASE),
    re.compile(r"saalse|\balex\b|samson", re.IGNORECASE),
    re.compile(r"\$\s?\d"),
    re.compile(
        r"https?://(?!(?:[a-z0-9-]+\.)?github\.com\b|example\.(?:com|org|net)\b)",
        re.IGNORECASE,
    ),
)
FILES = ("README.md", "pyproject.toml", ".gitignore")
TREES = ("src", "tests", "examples", "docs")


def _tracked() -> list[Path]:
    found = [ROOT / name for name in FILES]
    for tree in TREES:
        found += (
            path
            for path in (ROOT / tree).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in (".pyc", ".png")
        )
    return [path for path in found if path != Path(__file__).resolve()]


@pytest.mark.unit
def test_no_tracked_file_names_a_consumer():
    files = _tracked()
    assert ROOT / "src" / "carl_merge" / "queue.py" in files, "the walk is empty"
    hits = [
        f"{path.relative_to(ROOT)}:{number}: {mark.pattern}"
        for path in files
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for mark in MARKS
        if mark.search(line)
    ]
    assert hits == []
