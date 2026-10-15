"""The git reads behind the overlap decision. Git runs through `subprocess` only."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from carl_merge.config import Config
from carl_merge.overlap import Decision, decide


class OverlapError(RuntimeError):
    """Git could not produce the two file lists."""


@dataclass(frozen=True)
class Comparison:
    decision: Decision
    base_used: str
    base_sha: str


def _run(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args], capture_output=True, text=True
    )


def _git(repo_dir: Path, *args: str) -> str:
    result = _run(repo_dir, *args)
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no output"])[0]
        raise OverlapError(f"git {args[0]} failed: {detail}")
    return result.stdout.strip()


def _has_commit(repo_dir: Path, ref: str) -> bool:
    return _run(repo_dir, "cat-file", "-e", f"{ref}^{{commit}}").returncode == 0


def changed_files(repo_dir: Path, base: str, head: str) -> dict[str, str]:
    out = _git(repo_dir, "diff", "--no-renames", "--name-status", "-z", base, head)
    tokens = [token for token in out.split("\0") if token]
    if len(tokens) % 2:
        raise OverlapError("git diff printed an odd number of fields")
    return {tokens[i + 1]: tokens[i][:1] for i in range(0, len(tokens), 2)}


def compare(
    repo_dir: Path,
    head: str,
    base_head: str,
    tested_base: str | None,
    config: Config,
    *,
    number: int | None = None,
) -> Comparison:
    if not _has_commit(repo_dir, head) and number is not None:
        _run(repo_dir, "fetch", "origin", f"pull/{number}/head")
    if not _has_commit(repo_dir, base_head):
        _run(repo_dir, "fetch", "origin", config.base_branch)
    merge_base = _git(repo_dir, "merge-base", base_head, head)
    if tested_base and _has_commit(repo_dir, tested_base):
        base_for_base, used = tested_base, "tested base"
    else:
        base_for_base, used = merge_base, "merge-base"
    decision = decide(
        changed_files(repo_dir, merge_base, head),
        changed_files(repo_dir, base_for_base, base_head),
        shared_risk=config.shared_risk,
        added_together=config.added_together,
    )
    return Comparison(decision, used, base_for_base)
