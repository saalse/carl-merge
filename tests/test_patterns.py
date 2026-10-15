"""Pattern matching: the part before `/**` and after `**/` is a glob too."""

import fnmatch

import pytest

from carl_merge.config import ConfigError, parse
from carl_merge.overlap import path_matches

MINIMAL = """\
ready_label = "ready-to-merge"
base_branch = "main"
ci_workflow = "ci.yml"
"""

PATHS = (
    "models.py",
    "shop/models.py",
    "shop/models/order.py",
    "shop/models/sub/line.py",
    "docs/models/effort.md",
    "shop/migrations/0145_add_colour.py",
    "migrations/0001_initial.py",
    "shop/services.py",
    "pyproject.toml",
    "svc/pyproject.toml",
    ".github/workflows/ci.yml",
    "tests/billing/conftest.py",
    "docs/[draft]/notes.md",
)


def _before(path: str, pattern: str) -> bool:
    """`path_matches` as it was in 0.1.1, to show that working patterns keep
    their meaning."""
    if pattern.startswith("**/"):
        suffix = pattern[3:]
        return path == suffix or path.endswith("/" + suffix)
    if pattern.startswith("*/migrations/**"):
        return "/migrations/" in path or path.startswith("migrations/")
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, pattern) or path == pattern


@pytest.mark.unit
@pytest.mark.parametrize(
    "pattern, path",
    [
        ("*/models/**", "shop/models/order.py"),
        ("*/models/**", "shop/models/sub/line.py"),
        ("*/models/**", "a/b/models/c.py"),
        ("**/*.py", "shop/models.py"),
        ("**/*.py", "models.py"),
        ("**/migrations/*.py", "shop/migrations/0145_add_colour.py"),
        ("**/migrations/*.py", "migrations/0001_initial.py"),
        ("services/*/**", "services/api/app.py"),
    ],
)
def test_a_glob_before_or_after_double_star_matches(pattern: str, path: str):
    assert path_matches(path, pattern)


@pytest.mark.unit
@pytest.mark.parametrize(
    "pattern, path",
    [
        ("*/models/**", "shop/models.py"),
        ("*/models/**", "models/x.py"),
        ("**/*.py", "docs/guide.md"),
        ("**/migrations/*.py", "shop/migrations/README.md"),
        ("shop/models/**", "shop/models_util.py"),
        ("**/conftest.py", "tests/test_conftest.py"),
    ],
)
def test_and_still_misses_what_it_should(pattern: str, path: str):
    assert not path_matches(path, pattern)


@pytest.mark.unit
@pytest.mark.parametrize(
    "pattern",
    [
        "shop/models/**",
        ".github/workflows/**",
        "**/conftest.py",
        "**/pyproject.toml",
        "*/migrations/**",
        "*/migrations/*.py",
        "pyproject.toml",
        "shop/services.py",
        "docs/[draft]/**",
    ],
)
def test_a_pattern_that_worked_before_keeps_its_meaning(pattern: str):
    for path in PATHS:
        assert path_matches(path, pattern) == _before(path, pattern), path


@pytest.mark.unit
@pytest.mark.parametrize("pattern", ["/pyproject.toml", "./uv.lock", "/shop/**"])
def test_a_pattern_that_can_never_match_is_refused(pattern: str):
    text = MINIMAL + f'[[shared_risk]]\npattern = "{pattern}"\nreason = "x"\n'
    with pytest.raises(ConfigError, match="relative to the repository root"):
        parse(text)


@pytest.mark.unit
@pytest.mark.parametrize(
    "pattern, path",
    [
        ("*/migrations/**/*.py", "shop/migrations/README.md"),
        ("*/migrations/**.py", "shop/migrations/README.md"),
        ("docs/[draft]/**", "docs/d/notes.md"),
        ("**/[draft].md", "docs/d.md"),
    ],
)
def test_a_pattern_does_not_match_beyond_what_it_says(pattern: str, path: str):
    assert not path_matches(path, pattern)


@pytest.mark.unit
def test_a_bracketed_name_beside_double_star_is_literal():
    assert path_matches("docs/[draft]/notes.md", "docs/[draft]/**")
    assert path_matches("docs/[draft].md", "**/[draft].md")
