"""Locked: what an adopting repository reads and copies.

These check that the technical guide and the examples carry each required point. They
do not pin sentences.
"""

import importlib
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _text(relpath: str) -> str:
    path = ROOT / relpath
    assert path.is_file(), f"{relpath} does not exist"
    return path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_readme_names_the_bypass_mode_and_why_not_the_others():
    readme = _text("docs/guide.md")
    assert "pull_request" in readme
    for mode in ("exempt", "always"):
        assert re.search(rf"`{mode}`", readme), mode
    assert re.search(r"audit", readme, re.IGNORECASE)
    assert re.search(r"push", readme, re.IGNORECASE)


@pytest.mark.unit
def test_readme_names_the_app_token_and_its_permissions():
    readme = _text("docs/guide.md")
    assert re.search(r"GitHub App", readme)
    assert "installation" in readme.lower()
    for permission in (
        "Contents",
        "Pull requests",
        "Issues",
        "Actions",
        "Checks",
        "Workflows",
    ):
        assert permission in readme, permission


@pytest.mark.unit
def test_readme_states_the_blind_spot_and_both_backstops():
    readme = _text("docs/guide.md").lower()
    assert "file-level" in readme
    assert "semantic" in readme
    assert "workflow_dispatch" in readme or "dispatch" in readme
    assert "red" in readme


@pytest.mark.unit
def test_example_config_parses():
    assert importlib.util.find_spec("carl_merge") is not None, (
        "carl_merge does not exist"
    )
    config = importlib.import_module("carl_merge.config")
    parsed = config.parse(_text("examples/carl-merge.toml"))
    assert parsed.dry_run is True


@pytest.mark.unit
def test_example_workflow_is_serial_and_uses_the_app_token():
    workflow = _text("examples/carl-merge.yml")
    assert re.search(r"^concurrency:", workflow, re.MULTILINE)
    assert re.search(r"cancel-in-progress:\s*false", workflow)
    assert re.search(r"actions/create-github-app-token@[0-9a-f]{40}", workflow)
    assert "CARL_MERGE_TOKEN" in workflow
    assert (
        "GITHUB_TOKEN" not in workflow.split("CARL_MERGE_TOKEN", 1)[1].split("\n", 1)[0]
    )
    assert re.search(r"fetch-depth:\s*0", workflow)


@pytest.mark.unit
def test_the_cli_refuses_to_run_without_the_app_token(monkeypatch, tmp_path, capsys):
    assert importlib.util.find_spec("carl_merge") is not None, (
        "carl_merge does not exist"
    )
    cli = importlib.import_module("carl_merge.cli")
    config = tmp_path / "carl-merge.toml"
    config.write_text(_text("examples/carl-merge.toml"), encoding="utf-8")
    monkeypatch.delenv("CARL_MERGE_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "job-token-must-not-be-used")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/shop")
    assert cli.main(["run", "--config", str(config)]) == 2
    assert "CARL_MERGE_TOKEN" in capsys.readouterr().err
