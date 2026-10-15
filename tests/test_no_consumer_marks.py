"""Locked: carl-merge names none of the projects or people that use it.

A consumer's tracker cites carl-merge, never the other way round. The marks
are an issue reference (`#` and three or four digits), a decision id (`D` and
two digits), the first consumer's name in any spelling, its owner's names, a
money figure, and any URL host outside GitHub and the reserved example
domains.

One address is let through: carl-merge's own repository,
`https://github.com/saalse/carl-merge`, which the PyPI page needs for its logo
and links. It is let through only exactly so: the same owner in any other
address, or any other repository of that owner, is still a mark.

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
OWN_REPO = re.compile(r"https://github\.com/saalse/carl-merge(?![\w.-])")
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


def _marks(line: str) -> list[str]:
    line = OWN_REPO.sub("https://github.com", line)
    return [mark.pattern for mark in MARKS if mark.search(line)]


@pytest.mark.unit
def test_no_tracked_file_names_a_consumer():
    files = _tracked()
    assert ROOT / "src" / "carl_merge" / "queue.py" in files, "the walk is empty"
    hits = [
        f"{path.relative_to(ROOT)}:{number}: {pattern}"
        for path in files
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for pattern in _marks(line)
    ]
    assert hits == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "https://github.com/saalse/carl-merge",
        "[guide](https://github.com/saalse/carl-merge/blob/main/docs/guide.md)",
        'Source = "https://github.com/saalse/carl-merge"',
    ],
)
def test_own_repository_address_is_let_through(line):
    assert _marks(line) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "saalse",
        "github.com/saalse/carl-merge",
        "https://github.com/saalse",
        "https://github.com/saalse/other-repo",
        "https://github.com/saalse/carl-merge-fork",
        "https://github.com/saalse/carl-merge.git",
        "https://github.com/saalse/carl-merge by saalse",
        "https://saalse.github.io/carl-merge",
    ],
)
def test_any_other_mention_of_the_owner_is_a_mark(line):
    assert _marks(line) != []
