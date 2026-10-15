"""Why the tested base must be the one the checks ran against.

A base the checks did not run against can hide a change: here the base branch
adds a shared-risk file after an older run and reverts it later, so a diff from
the older base sees nothing while a diff from the tested base sees the revert.
"""

import subprocess
from pathlib import Path

import pytest

from carl_merge import config, gitdiff

CONFIG = config.parse(
    'ready_label = "r"\nbase_branch = "main"\nci_workflow = "ci.yml"\n'
    '[[shared_risk]]\npattern = "uv.lock"\nreason = "locked dependencies"\n'
)


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def commit(repo: Path, path: str, text: str | None) -> str:
    target = repo / path
    if text is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"change {path}")
    return git(repo, "rev-parse", "HEAD")


@pytest.mark.unit
def test_a_reverted_base_change_is_seen_only_from_the_tested_base(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "ci@example.invalid")
    git(repo, "config", "user.name", "CI")
    git(repo, "config", "commit.gpgsign", "false")
    older = commit(repo, "README.md", "start\n")
    git(repo, "checkout", "-q", "-b", "feature")
    head = commit(repo, "shop/views.py", "feature\n")
    git(repo, "checkout", "-q", "main")
    tested_against = commit(repo, "uv.lock", "pinned\n")
    base_now = commit(repo, "uv.lock", None)

    from_older = gitdiff.compare(repo, head, base_now, older, CONFIG)
    from_tested = gitdiff.compare(repo, head, base_now, tested_against, CONFIG)

    assert from_older.decision.verdict == "disjoint"
    assert from_tested.decision.verdict == "wait"
    assert ("base", "uv.lock", "locked dependencies") in (
        from_tested.decision.shared_risk
    )
