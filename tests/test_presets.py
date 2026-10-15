"""Opt-in presets: named sets of `shared_risk` rules added to the adopter's own."""

import re
from pathlib import Path

import pytest

from carl_merge.config import ConfigError, parse
from carl_merge.overlap import decide, path_matches
from carl_merge.presets import PRESETS

ROOT = Path(__file__).resolve().parents[1]

MINIMAL = """\
ready_label = "ready-to-merge"
base_branch = "main"
ci_workflow = "ci.yml"
"""

ALL_RULES = [(name, rule) for name, rules in PRESETS.items() for rule in rules]


def _ids(item) -> str:
    return f"{item[0]}:{item[1].pattern}"


@pytest.mark.unit
@pytest.mark.parametrize("item", ALL_RULES, ids=_ids)
def test_each_preset_rule_matches_its_examples(item):
    _name, rule = item
    assert rule.matches, "every preset rule names a path it must match"
    for path in rule.matches:
        assert path_matches(path, rule.pattern), path
    for path in rule.misses:
        assert not path_matches(path, rule.pattern), path


@pytest.mark.unit
def test_patterns_are_unique_within_a_preset():
    for name, rules in PRESETS.items():
        patterns = [rule.pattern for rule in rules]
        assert len(patterns) == len(set(patterns)), name


@pytest.mark.unit
def test_no_presets_by_default():
    config = parse(MINIMAL)
    assert config.presets == ()
    assert config.shared_risk == ()


@pytest.mark.unit
def test_a_preset_adds_its_rules_after_the_own_ones():
    config = parse(
        MINIMAL
        + 'presets = ["github-actions"]\n'
        + '[[shared_risk]]\npattern = "shop/services.py"\nreason = "write facade"\n'
    )
    assert config.presets == ("github-actions",)
    assert [(r.pattern, r.reason) for r in config.shared_risk] == [
        ("shop/services.py", "write facade"),
        (".github/workflows/**", "CI workflows; preset github-actions"),
        (".github/actions/**", "CI composite actions; preset github-actions"),
    ]


@pytest.mark.unit
def test_an_own_rule_with_the_same_pattern_wins():
    config = parse(
        MINIMAL
        + 'presets = ["python"]\n'
        + '[[shared_risk]]\npattern = "**/uv.lock"\nreason = "the lockfile"\n'
    )
    reasons = [r.reason for r in config.shared_risk if r.pattern == "**/uv.lock"]
    assert reasons == ["the lockfile"]


@pytest.mark.unit
def test_presets_are_added_in_the_listed_order():
    config = parse(MINIMAL + 'presets = ["github-actions", "django"]\n')
    patterns = [r.pattern for r in config.shared_risk]
    assert patterns == [r.pattern for r in PRESETS["github-actions"]] + [
        r.pattern for r in PRESETS["django"]
    ]


@pytest.mark.unit
def test_the_wait_reason_names_the_preset():
    config = parse(MINIMAL + 'presets = ["django"]\n')
    decision = decide(
        {"shop/management/commands/import_orders.py": "A"},
        {"shop/models/order.py": "M"},
        shared_risk=config.shared_risk,
        added_together=config.added_together,
    )
    assert decision.verdict == "wait"
    assert (
        "  - shop/models/order.py (Django model definitions; preset django)"
        in decision.reason_lines()
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "line, message",
    [
        ('presets = "django"\n', "presets must be a list of names"),
        ("presets = [1]\n", "presets must be a list of names"),
        (
            'presets = ["djagno"]\n',
            re.escape("unknown preset: djagno (known: django, github-actions, python)"),
        ),
        ('presets = ["django", "django"]\n', "preset listed twice: django"),
    ],
)
def test_a_wrong_presets_value_is_refused(line: str, message: str):
    with pytest.raises(ConfigError, match=message):
        parse(MINIMAL + line)


@pytest.mark.unit
def test_an_empty_list_is_no_presets():
    assert parse(MINIMAL + "presets = []\n").shared_risk == ()


def _guide_tables() -> dict[str, list[str]]:
    """The patterns the guide lists under each `#### \\`name\\`` heading."""
    guide = (ROOT / "docs/guide.md").read_text(encoding="utf-8")
    tables: dict[str, list[str]] = {}
    current = None
    for line in guide.splitlines():
        heading = re.match(r"#### `([a-z-]+)`$", line)
        if heading:
            current = tables.setdefault(heading.group(1), [])
        elif line.startswith("#"):
            current = None
        elif current is not None:
            cell = re.match(r"\| `([^`]+)` \|", line)
            if cell:
                current.append(cell.group(1))
    return tables


@pytest.mark.unit
def test_the_guide_lists_exactly_the_preset_patterns():
    assert _guide_tables() == {
        name: [rule.pattern for rule in rules] for name, rules in PRESETS.items()
    }


@pytest.mark.unit
def test_the_example_config_uses_presets():
    config = parse((ROOT / "examples/carl-merge.toml").read_text(encoding="utf-8"))
    assert config.presets
