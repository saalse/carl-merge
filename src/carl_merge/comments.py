"""Comment bodies, and the marker that makes a comment post once per head."""

from __future__ import annotations

import re
from collections.abc import Iterable

MARKER_RE = re.compile(r"<!-- carl-merge:([a-z-]+):([0-9A-Za-z._-]+) -->")
MAX_LINES = 20
MAX_REASON = 300

_LIVE = {
    "conflict": (
        "Carl Mergé: this branch conflicts with {base}. I removed {label}; "
        "add it back when the conflict is fixed."
    ),
    "merge": "Carl Mergé: merged {head} into {base}.",
    "bypass": (
        "Carl Mergé: merged {head} into {base} without updating the branch: "
        "nothing it changed overlaps what {base} gained since its checks ran."
    ),
    "update": (
        "Carl Mergé: {base} gained changes this PR needs to be tested with. "
        "I updated the branch to {new_head} and will merge when its required "
        "checks pass."
    ),
    "cannot-decide": (
        "Carl Mergé: I cannot decide this PR yet: {reason}. I left {label} on it."
    ),
    "dispatch": "Carl Mergé: I will run {workflow} on {base} after this pass.",
    "base-red": (
        "Carl Mergé: I am not merging this PR into {base} while the latest "
        "{workflow} run on {base} is red: {run_url}. I left {label} on it."
    ),
    "red-after-bypass": (
        "Carl Mergé: the {workflow} run on {base} after this merge is red: {run_url}."
    ),
}
_DRY = {
    "conflict": (
        "Carl Mergé would hand this back: it conflicts with {base}. "
        "I would remove {label}."
    ),
    "merge": "Carl Mergé would merge {head} into {base}.",
    "bypass": (
        "Carl Mergé would merge {head} into {base} without updating the branch: "
        "nothing it changed overlaps what {base} gained since its checks ran."
    ),
    "update": (
        "Carl Mergé would update the branch, because {base} gained changes "
        "this PR needs to be tested with, and merge when its required checks pass."
    ),
    "cannot-decide": (
        "Carl Mergé cannot decide this PR yet: {reason}. I would leave {label} on it."
    ),
    "dispatch": "Carl Mergé would run {workflow} on {base} after this pass.",
    "base-red": (
        "Carl Mergé would not merge this PR into {base} while the latest "
        "{workflow} run on {base} is red: {run_url}. I would leave {label} on it."
    ),
    "red-after-bypass": (
        "Carl Mergé would point out that the {workflow} run on {base} after this "
        "merge is red: {run_url}."
    ),
}


def key(action: str, head: str, *, new_head: str = "", run_id: int = 0) -> str:
    """What a comment's marker carries: the head, or what it is posted once per."""
    if action == "update":
        return new_head or head
    return f"{head}.{run_id}" if run_id else head


def marker(action: str, head: str, *, dry: bool = False) -> str:
    return f"<!-- carl-merge:{'dry-' if dry else ''}{action}:{head} -->"


def markers(bodies: Iterable[str]) -> set[tuple[str, str]]:
    """Every `(action, head)` the given comment bodies carry."""
    return {m for body in bodies for m in MARKER_RE.findall(body)}


def one_line(text: str, limit: int = MAX_REASON) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def render(
    action: str,
    *,
    head: str,
    base: str,
    label: str = "",
    dry: bool = False,
    new_head: str = "",
    reason: str = "",
    workflow: str = "",
    lines: Iterable[str] = (),
    run_id: int = 0,
    run_url: str = "",
) -> str:
    template = (_DRY if dry else _LIVE)[action]
    first = template.format(
        head=head[:8],
        base=base,
        label=label,
        new_head=new_head[:8],
        reason=one_line(reason),
        workflow=workflow,
        run_url=run_url,
    )
    if dry:
        first = f"Dry run: {first}"
    detail = [one_line(line, 200) for line in list(lines)[:MAX_LINES]]
    marked = key(action, head, new_head=new_head, run_id=run_id)
    return "\n".join([first, *detail, "", marker(action, marked, dry=dry)])
