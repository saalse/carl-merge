"""The overlap decision over two `{path: status}` maps. Pure, no I/O."""

from __future__ import annotations

import fnmatch
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass

from carl_merge.config import Rule


def path_matches(path: str, pattern: str) -> bool:
    if pattern.startswith("**/"):
        suffix = pattern[3:]
        return path == suffix or path.endswith("/" + suffix)
    if pattern.startswith("*/migrations/**"):
        return "/migrations/" in path or path.startswith("migrations/")
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, pattern) or path == pattern


@dataclass(frozen=True)
class Decision:
    verdict: str
    overlapping: tuple[str, ...] = ()
    shared_risk: tuple[tuple[str, str, str], ...] = ()
    added_together: tuple[tuple[str, str], ...] = ()

    def reason_lines(self) -> list[str]:
        lines: list[str] = []
        if self.overlapping:
            lines.append("Files changed on both sides:")
            lines.extend(f"  - {path}" for path in self.overlapping)
        for side, title in (
            ("pr", "Shared-risk paths changed by the pull request:"),
            ("base", "Shared-risk paths changed on the base branch:"),
        ):
            hits = [(p, r) for s, p, r in self.shared_risk if s == side]
            if hits:
                lines.append(title)
                lines.extend(f"  - {path} ({reason})" for path, reason in hits)
        if self.added_together:
            lines.append("Files added on both sides in the same directory:")
            lines.extend(
                f"  - {where} ({reason})" for where, reason in self.added_together
            )
        if not lines:
            lines.append(
                "No file overlap and no shared-risk path since its checks ran."
            )
        return lines


def _added_directories(files: Mapping[str, str], rule: Rule) -> set[str]:
    return {
        posixpath.dirname(path)
        for path, status in files.items()
        if status == "A" and path_matches(path, rule.pattern)
    }


def decide(
    pr_files: Mapping[str, str],
    base_files: Mapping[str, str],
    *,
    shared_risk: tuple[Rule, ...],
    added_together: tuple[Rule, ...],
) -> Decision:
    overlapping = tuple(sorted(set(pr_files) & set(base_files)))
    risky = []
    for side, files in (("pr", pr_files), ("base", base_files)):
        for path in files:
            for rule in shared_risk:
                if path_matches(path, rule.pattern):
                    risky.append((side, path, rule.reason))
                    break
    together = set()
    for rule in added_together:
        both = _added_directories(pr_files, rule) & _added_directories(base_files, rule)
        together.update((where, rule.reason) for where in both)
    if not (overlapping or risky or together):
        return Decision("disjoint")
    return Decision(
        "wait",
        overlapping=overlapping,
        shared_risk=tuple(sorted(risky)),
        added_together=tuple(sorted(together)),
    )
