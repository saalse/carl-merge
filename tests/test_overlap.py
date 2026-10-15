"""Locked: the overlap decision, with every pattern supplied by config.

The git cases are ported from the checker this package replaces. Its paths are
renamed to a made-up shop project; the structure of each case is unchanged.
"""

import importlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

TODAY = """\
ready_label = "ready-to-merge"
base_branch = "main"
ci_workflow = "ci.yml"
dry_run = false

[[shared_risk]]
pattern = "*/migrations/**"
reason = "migration numbers"
[[shared_risk]]
pattern = "shop/models/**"
reason = "model definitions and schema"
[[shared_risk]]
pattern = "shop/services.py"
reason = "write facade"
[[shared_risk]]
pattern = "shop/selectors.py"
reason = "read facade"
[[shared_risk]]
pattern = "pyproject.toml"
reason = "import contracts and dependencies"
[[shared_risk]]
pattern = "uv.lock"
reason = "locked dependencies"
[[shared_risk]]
pattern = "site/settings.py"
reason = "installed apps and middleware ordering"
[[shared_risk]]
pattern = "**/conftest.py"
reason = "fixtures read across suites"
[[shared_risk]]
pattern = ".github/workflows/**"
reason = "CI workflow controls"
[[shared_risk]]
pattern = ".github/scripts/**"
reason = "CI scripts"
[[shared_risk]]
pattern = "docs/decisions.md"
reason = "decision numbers"
[[shared_risk]]
pattern = "tests/test_invariants.py"
reason = "invariants baseline"
"""

MIGRATIONS = 'pattern = "*/migrations/*.py"\nreason = "migration numbers"\n'


def _module(name: str):
    full = f"carl_merge.{name}"
    assert importlib.util.find_spec("carl_merge") is not None, (
        "carl_merge does not exist"
    )
    assert importlib.util.find_spec(full) is not None, f"{full} does not exist"
    return importlib.import_module(full)


def _today():
    return _module("config").parse(TODAY)


def _rules(text: str):
    return _module("config").parse(TODAY.split("[[shared_risk]]")[0] + text)


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return res.stdout.strip()


def commit_file(repo: Path, relpath: str, content: str = "content\n") -> str:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"edit {relpath}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "ci@example.invalid")
    git(path, "config", "user.name", "CI")
    git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("initial\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "initial commit")
    return path


def _branch(repo: Path, pr_path: str, main_path: str) -> tuple[str, str, str]:
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    head = commit_file(repo, pr_path, "feature\n")
    git(repo, "checkout", "-q", "main")
    main = commit_file(repo, main_path, "main edit\n")
    return base, head, main


def _compare(repo: Path, head: str, main: str, tested_base: str | None):
    gitdiff = _module("gitdiff")
    return gitdiff.compare(repo, head, main, tested_base, _today())


@pytest.mark.unit
def test_disjoint_pr(repo: Path):
    base, head, main = _branch(repo, "reports/unrelated.py", "shop/views/grid.py")
    result = _compare(repo, head, main, base)
    assert result.decision.verdict == "disjoint"
    assert result.base_used == "tested base"


@pytest.mark.unit
def test_overlapping_file_waits(repo: Path):
    base, head, main = _branch(repo, "reports/dashboard.py", "reports/dashboard.py")
    result = _compare(repo, head, main, base)
    assert result.decision.verdict == "wait"
    assert result.decision.overlapping == ("reports/dashboard.py",)


@pytest.mark.unit
def test_shared_risk_path_on_pr_side_waits(repo: Path):
    base, head, main = _branch(repo, "shop/services.py", "reports/dashboard.py")
    result = _compare(repo, head, main, base)
    assert result.decision.verdict == "wait"
    assert ("pr", "shop/services.py", "write facade") in result.decision.shared_risk


@pytest.mark.unit
def test_shared_risk_path_on_base_side_waits(repo: Path):
    base, head, main = _branch(repo, "reports/unrelated.py", "shop/services.py")
    result = _compare(repo, head, main, base)
    assert result.decision.verdict == "wait"
    assert ("base", "shop/services.py", "write facade") in result.decision.shared_risk


@pytest.mark.unit
def test_renamed_file_with_base_edit_waits(repo: Path):
    commit_file(repo, "reports/helper.py", "original\n")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    git(repo, "mv", "reports/helper.py", "reports/renamed_helper.py")
    git(repo, "commit", "-qm", "rename helper")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    main = commit_file(repo, "reports/helper.py", "original with edit\n")
    result = _compare(repo, head, main, base)
    assert result.decision.verdict == "wait"
    assert "reports/helper.py" in result.decision.overlapping


@pytest.mark.unit
def test_unreadable_tested_base_falls_back_to_merge_base(repo: Path):
    _base, head, main = _branch(repo, "reports/unrelated.py", "shop/views/grid.py")
    for tested in (None, "0" * 40):
        result = _compare(repo, head, main, tested)
        assert result.decision.verdict == "disjoint"
        assert result.base_used == "merge-base"


@pytest.mark.unit
def test_git_failure_is_an_overlap_error(repo: Path):
    gitdiff = _module("gitdiff")
    with pytest.raises(gitdiff.OverlapError):
        gitdiff.compare(repo, "f" * 40, "main", None, _today())


@pytest.mark.unit
@pytest.mark.parametrize(
    "path, expected_match",
    [
        ("shop/migrations/0009_foo.py", True),
        ("billing/migrations/0010_bar.py", True),
        ("shop/models/order.py", True),
        ("shop/services.py", True),
        ("shop/selectors.py", True),
        ("pyproject.toml", True),
        ("uv.lock", True),
        ("site/settings.py", True),
        ("conftest.py", True),
        ("tests/conftest.py", True),
        ("tests/billing/conftest.py", True),
        (".github/workflows/ci.yml", True),
        (".github/scripts/classify-diff.sh", True),
        ("docs/decisions.md", True),
        ("tests/test_invariants.py", True),
        ("reports/dashboard.py", False),
        ("tools/overlap.py", False),
        ("tests/tools/test_overlap.py", False),
        ("docs/parallel-work.md", False),
    ],
)
def test_shared_risk_patterns_come_from_config(path: str, expected_match: bool):
    overlap = _module("overlap")
    rules = _today().shared_risk
    matched = [rule for rule in rules if overlap.path_matches(path, rule.pattern)]
    assert bool(matched) is expected_match


@pytest.mark.unit
def test_no_config_patterns_means_only_file_overlap_blocks():
    overlap = _module("overlap")
    decision = overlap.decide(
        {"shop/services.py": "M", "pyproject.toml": "M"},
        {"shop/models/order.py": "M", "uv.lock": "M"},
        shared_risk=(),
        added_together=(),
    )
    assert decision.verdict == "disjoint"


@pytest.mark.unit
def test_both_sides_add_a_migration_in_one_app_waits():
    overlap = _module("overlap")
    rules = _rules("[[added_together]]\n" + MIGRATIONS).added_together
    decision = overlap.decide(
        {"shop/migrations/0145_add_colour.py": "A", "shop/forms.py": "M"},
        {"shop/migrations/0145_add_size.py": "A"},
        shared_risk=(),
        added_together=rules,
    )
    assert decision.verdict == "wait"
    assert decision.added_together == (("shop/migrations", "migration numbers"),)


@pytest.mark.unit
def test_migrations_added_in_different_apps_do_not_wait():
    overlap = _module("overlap")
    rules = _rules("[[added_together]]\n" + MIGRATIONS).added_together
    decision = overlap.decide(
        {"shop/migrations/0145_add_colour.py": "A"},
        {"billing/migrations/0031_add_due.py": "A"},
        shared_risk=(),
        added_together=rules,
    )
    assert decision.verdict == "disjoint"


@pytest.mark.unit
def test_one_side_adding_a_migration_does_not_wait():
    overlap = _module("overlap")
    rules = _rules("[[added_together]]\n" + MIGRATIONS).added_together
    decision = overlap.decide(
        {"shop/migrations/0145_add_colour.py": "A"},
        {"shop/migrations/0001_initial.py": "M", "shop/views.py": "M"},
        shared_risk=(),
        added_together=rules,
    )
    assert decision.verdict == "disjoint"


@pytest.mark.unit
def test_two_sided_rule_is_read_from_git_status(repo: Path):
    gitdiff = _module("gitdiff")
    config = _rules("[[added_together]]\n" + MIGRATIONS)
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    head = commit_file(repo, "shop/migrations/0145_add_colour.py")
    git(repo, "checkout", "-q", "main")
    main = commit_file(repo, "shop/migrations/0145_add_size.py")
    result = gitdiff.compare(repo, head, main, base, config)
    assert result.decision.verdict == "wait"
    assert result.decision.added_together == (("shop/migrations", "migration numbers"),)
