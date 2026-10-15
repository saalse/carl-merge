"""Locked: one TOML file in the adopting repository configures the bot."""

import importlib
import importlib.util
from pathlib import Path

import pytest

FULL = """\
ready_label = "ready-to-merge"
base_branch = "trunk"
ci_workflow = "full-suite.yml"
merge_method = "squash"
dry_run = false
required_checks = ["lint", "unit"]

[[shared_risk]]
pattern = "pyproject.toml"
reason = "dependencies"

[[added_together]]
pattern = "*/migrations/*.py"
reason = "migration numbers"
"""

MINIMAL = """\
ready_label = "ready-to-merge"
base_branch = "main"
ci_workflow = "ci.yml"
"""


def _config():
    assert importlib.util.find_spec("carl_merge") is not None, (
        "carl_merge does not exist"
    )
    assert importlib.util.find_spec("carl_merge.config") is not None, (
        "carl_merge.config does not exist"
    )
    return importlib.import_module("carl_merge.config")


@pytest.mark.unit
def test_every_key_is_read():
    config = _config().parse(FULL)
    assert config.ready_label == "ready-to-merge"
    assert config.base_branch == "trunk"
    assert config.ci_workflow == "full-suite.yml"
    assert config.merge_method == "squash"
    assert config.dry_run is False
    assert config.required_checks == ("lint", "unit")
    assert [(r.pattern, r.reason) for r in config.shared_risk] == [
        ("pyproject.toml", "dependencies")
    ]
    assert [(r.pattern, r.reason) for r in config.added_together] == [
        ("*/migrations/*.py", "migration numbers")
    ]


@pytest.mark.unit
def test_defaults_are_safe():
    config = _config().parse(MINIMAL)
    assert config.dry_run is True
    assert config.merge_method == "merge"
    assert config.required_checks == ()
    assert config.shared_risk == ()
    assert config.added_together == ()


@pytest.mark.unit
def test_load_reads_a_file(tmp_path: Path):
    path = tmp_path / "carl-merge.toml"
    path.write_text(FULL, encoding="utf-8")
    assert _config().load(path).base_branch == "trunk"


@pytest.mark.unit
@pytest.mark.parametrize("key", ["ready_label", "base_branch", "ci_workflow"])
def test_a_missing_required_key_is_refused(key: str):
    config = _config()
    text = "\n".join(line for line in MINIMAL.splitlines() if not line.startswith(key))
    with pytest.raises(config.ConfigError, match=key):
        config.parse(text)


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [
        "dryrun = false\n",
        'merge_method = "rebase"\n',
        'dry_run = "no"\n',
        '[[shared_risk]]\npattern = "uv.lock"\n',
        '[[added_together]]\nreason = "migration numbers"\n',
        '[[shared_risk]]\npattern = ""\nreason = "empty"\n',
    ],
)
def test_a_wrong_or_unknown_value_is_refused(extra: str):
    config = _config()
    with pytest.raises(config.ConfigError):
        config.parse(MINIMAL + extra)
