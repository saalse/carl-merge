"""What the queue needs from a forge, and the errors a forge raises."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ForgeError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HeadMoved(ForgeError):
    """The pull request head is no longer the one the caller pinned."""


class UpdateConflict(ForgeError):
    """Updating the branch hit a merge conflict."""


class MergeRefused(ForgeError):
    """The forge will not merge this pull request."""


class MergeTimeout(ForgeError):
    """The forge did not finish a merge in the time allowed."""


@dataclass(frozen=True)
class Pull:
    number: int
    head_sha: str
    head_ref: str
    draft: bool
    mergeable: bool | None
    behind: bool
    fork: bool
    merged: bool = False


class Forge(Protocol):
    def ready_pulls(self, label: str, base: str) -> list[int]: ...

    def pull(self, number: int) -> Pull: ...

    def base_head(self, branch: str) -> str: ...

    def base_red(self, workflow: str, branch: str) -> bool: ...

    def checks(self, number: int, sha: str) -> str: ...

    def tested_base(self, number: int, sha: str) -> str | None: ...

    def comment_bodies(self, number: int) -> list[str]: ...

    def comment(self, number: int, body: str) -> None: ...

    def closed_with_label(self, label: str) -> list[int]: ...

    def add_label(self, number: int, label: str) -> None: ...

    def remove_label(self, number: int, label: str) -> None: ...

    def merge(self, number: int, sha: str, method: str, bypass: bool) -> str: ...

    def update_branch(self, number: int, expected_head_sha: str) -> None: ...

    def cancel_runs(self, number: int, head_ref: str) -> int: ...

    def dispatch(self, workflow: str, branch: str) -> None: ...
