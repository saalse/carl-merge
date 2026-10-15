"""One pass over the ready queue: oldest first, one pull request at a time."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from carl_merge import comments
from carl_merge.config import Config
from carl_merge.forge import (
    Forge,
    HeadMoved,
    MergeRefused,
    MergeTimeout,
    Pull,
    UpdateConflict,
)
from carl_merge.gitdiff import OverlapError
from carl_merge.overlap import Decision

MERGEABLE_READS = 5
MERGEABLE_WAIT = 2
HEAD_READS = 10
HEAD_WAIT = 3
OWED_LABEL = "carl-merge-ci-owed"
UNKNOWN_TESTED_BASE = (
    "Its required checks cannot be tied to one tested base, so it is tested "
    "again rather than merged without an update."
)


class Action(StrEnum):
    conflict = "conflict"
    merge = "merge"
    bypass = "bypass"
    update = "update"
    cannot_decide = "cannot-decide"
    hold = "hold"
    skip = "skip"
    base_red = "base-red"
    head_moved = "head-moved"
    base_moved = "base-moved"
    landed = "landed"


@dataclass
class PassReport:
    actions: list[tuple[int, Action]] = field(default_factory=list)
    stopped: Action | None = None
    dispatched: bool = False


Overlap = Callable[[Pull, str, "str | None"], Decision]


class _Pass:
    def __init__(
        self,
        forge: Forge,
        config: Config,
        overlap: Overlap,
        sleep: Callable[[float], None],
        log: Callable[[str], None] | None,
    ) -> None:
        self.forge = forge
        self.config = config
        self.overlap = overlap
        self.sleep = sleep
        self.log = log or (lambda line: None)
        self.report = PassReport()
        # Bypass merges whose dispatch is still owed, each with its comment's
        # reason lines: made in this pass, or found landed after an earlier one.
        self.owed: list[tuple[Pull, list[str]]] = []
        self.last_dry_bypass: Pull | None = None

    # -- records ---------------------------------------------------------

    def _record(self, pull: Pull, action: Action, reason: str = "") -> None:
        self.report.actions.append((pull.number, action))
        first = (reason.splitlines() or [""])[0]
        self.log(f"#{pull.number} {action} {pull.head_sha[:8]} {first}".rstrip())

    def _stop(self, action: Action, reason: str) -> bool:
        self.report.stopped = action
        self.log(f"{action} {reason}")
        return True

    def _comment(
        self,
        pull: Pull,
        action: str,
        *,
        reason: str = "",
        new_head: str = "",
        workflow: str = "",
        lines: Iterable[str] = (),
    ) -> None:
        dry = self.config.dry_run
        recorded = comments.markers(self.forge.comment_bodies(pull.number))
        if (("dry-" if dry else "") + action, new_head or pull.head_sha) in recorded:
            return
        body = comments.render(
            action,
            head=pull.head_sha,
            base=self.config.base_branch,
            label=self.config.ready_label,
            dry=dry,
            new_head=new_head,
            reason=reason,
            workflow=workflow,
            lines=lines,
        )
        self.forge.comment(pull.number, body)

    # -- the pass --------------------------------------------------------

    def run(self) -> None:
        config = self.config
        if not config.dry_run:
            self._settle_owed()
        if self.forge.base_red(config.ci_workflow, config.base_branch):
            self._stop(Action.base_red, "the base branch's latest run is red")
            return
        numbers = self.forge.ready_pulls(config.ready_label, config.base_branch)
        if not numbers:
            self.log("queue empty")
            return
        for number in numbers:
            if self._one(number):
                return

    def _settle_owed(self) -> None:
        """Count each labelled bypass that has since landed without a dispatch."""
        for number in self.forge.closed_with_label(OWED_LABEL):
            pull = self.forge.pull(number)
            recorded = comments.markers(self.forge.comment_bodies(number))
            done = {("bypass", pull.head_sha), ("merge", pull.head_sha)} & recorded
            if not pull.merged or done:
                self.forge.remove_label(number, OWED_LABEL)
                continue
            self.owed.append((pull, []))
            self._record(pull, Action.landed, "a bypass merge landed after the polls")

    def _read(self, number: int) -> Pull:
        pull = self.forge.pull(number)
        for _ in range(MERGEABLE_READS - 1):
            if pull.mergeable is not None:
                break
            self.sleep(MERGEABLE_WAIT)
            pull = self.forge.pull(number)
        return pull

    def _one(self, number: int) -> bool:
        """Handle one pull request. True means the pass stops here."""
        pull = self._read(number)
        if pull.draft or pull.fork:
            kind = "it is a draft" if pull.draft else "it comes from a fork"
            return self._cannot_decide(pull, kind)
        if pull.mergeable is None:
            self._record(pull, Action.skip, "mergeability is not known yet")
            return False
        if not pull.mergeable:
            return self._conflict(pull)
        state = self.forge.checks(number, pull.head_sha)
        if state in ("unknown", "red"):
            return self._cannot_decide(pull, f"its required checks are {state}")
        if state == "pending":
            live = ("update", pull.head_sha) in comments.markers(
                self.forge.comment_bodies(number)
            )
            if live:
                self._record(pull, Action.hold, "waiting for its checks after update")
                return self._stop(Action.hold, f"#{number} is next in line")
            self._record(pull, Action.skip, "required checks are pending")
            return False
        base = self.forge.base_head(self.config.base_branch)
        if not pull.behind:
            return self._merge(pull, base, bypass=False)
        tested = self.forge.tested_base(number, pull.head_sha)
        if tested is None:
            # Without the one base its checks ran against, any other base can
            # hide a change, so it is tested again instead of bypassed.
            return self._update(pull, [UNKNOWN_TESTED_BASE])
        try:
            decision = self.overlap(pull, base, tested)
        except OverlapError as exc:
            return self._cannot_decide(pull, str(exc))
        if decision.verdict == "disjoint":
            return self._merge(pull, base, bypass=True, decision=decision)
        return self._update(pull, decision.reason_lines())

    # -- the rows --------------------------------------------------------

    def _conflict(self, pull: Pull) -> bool:
        self._comment(pull, "conflict")
        if not self.config.dry_run:
            self.forge.remove_label(pull.number, self.config.ready_label)
        self._record(pull, Action.conflict)
        return False

    def _cannot_decide(self, pull: Pull, reason: str) -> bool:
        self._comment(pull, "cannot-decide", reason=reason)
        self._record(pull, Action.cannot_decide, reason)
        return False

    def _guard(self, pull: Pull, base: str) -> bool | None:
        """Re-read before a merge. None means go on; a bool ends this PR."""
        config = self.config
        if self.forge.base_red(config.ci_workflow, config.base_branch):
            return self._stop(Action.base_red, "the base branch's latest run is red")
        if self.forge.pull(pull.number).head_sha != pull.head_sha:
            self._record(pull, Action.head_moved, "the head moved before the merge")
            return False
        if self.forge.base_head(config.base_branch) != base:
            self._record(pull, Action.base_moved, "the base moved before the merge")
            return False
        return None

    def _merge(
        self, pull: Pull, base: str, *, bypass: bool, decision: Decision | None = None
    ) -> bool:
        ended = self._guard(pull, base)
        if ended is not None:
            return ended
        action = Action.bypass if bypass else Action.merge
        lines = decision.reason_lines() if decision else []
        if self.config.dry_run:
            self._comment(pull, str(action), lines=lines)
            self._record(pull, action, "dry run")
            if bypass:
                self.last_dry_bypass = pull
            return False
        if bypass:
            # Owed until its dispatch succeeds: GitHub can finish the merge
            # after the polls, or the job can end first, and the label is how a
            # later pass finds it. A refusal keeps it too, since an earlier
            # request may still land; the settle decides once the PR closes.
            self.forge.add_label(pull.number, OWED_LABEL)
        try:
            self.forge.merge(
                pull.number, pull.head_sha, self.config.merge_method, bypass
            )
        except HeadMoved:
            self._record(pull, Action.head_moved, "the head moved during the merge")
            return False
        except MergeRefused as exc:
            return self._cannot_decide(pull, str(exc))
        except MergeTimeout:
            self._record(pull, Action.hold, "the merge has not finished")
            return self._stop(Action.hold, f"#{pull.number} merge is still running")
        self._record(pull, action)
        if bypass:
            # Owed before anything else can fail; its marker waits for the
            # dispatch, or a failed one would be lost.
            self.owed.append((pull, lines))
        self.forge.cancel_runs(pull.number, pull.head_ref)
        if not bypass:
            self._comment(pull, str(action), lines=lines)
        return False

    def _update(self, pull: Pull, lines: list[str]) -> bool:
        config = self.config
        if self.forge.base_red(config.ci_workflow, config.base_branch):
            return self._stop(Action.base_red, "the base branch's latest run is red")
        if config.dry_run:
            self._comment(pull, "update", lines=lines)
            self._record(pull, Action.update, "dry run")
            return False
        try:
            self.forge.update_branch(pull.number, pull.head_sha)
        except HeadMoved:
            self._record(pull, Action.head_moved, "the head moved before the update")
            return False
        except UpdateConflict:
            return self._conflict(pull)
        new_head = self._new_head(pull)
        if new_head is None:
            self._record(pull, Action.hold, "the update has not produced a new head")
            return self._stop(Action.hold, f"#{pull.number} update is still running")
        self._comment(pull, "update", new_head=new_head, lines=lines)
        self._record(pull, Action.update, f"now {new_head[:8]}")
        return self._stop(Action.hold, f"#{pull.number} is next in line")

    def _new_head(self, pull: Pull) -> str | None:
        for attempt in range(HEAD_READS):
            head = self.forge.pull(pull.number).head_sha
            if head != pull.head_sha:
                return head
            if attempt < HEAD_READS - 1:
                self.sleep(HEAD_WAIT)
        return None

    # -- the end ---------------------------------------------------------

    def finish(self, *, failing: bool = False) -> None:
        config = self.config
        if self.owed and not config.dry_run:
            try:
                self.forge.dispatch(config.ci_workflow, config.base_branch)
            except Exception:
                if not failing:
                    raise
                return
            self.report.dispatched = True
            self.log(f"dispatch {config.ci_workflow} {config.base_branch}")
            self._settle_dispatched(failing)
        elif self.last_dry_bypass is not None and not failing:
            self._comment(self.last_dry_bypass, "dispatch", workflow=config.ci_workflow)

    def _settle_dispatched(self, failing: bool) -> None:
        """Unlabel and mark each owed pull request, now that its run is out.

        This waits for the dispatch, so a failed one leaves the labels for the
        next pass to retry. Either write alone stops a second dispatch, the
        label's absence or the marker, so each is tried even when the other
        fails, and the first error is raised only once every write was tried.
        """
        errors: list[Exception] = []
        for pull, lines in self.owed:
            try:
                self.forge.remove_label(pull.number, OWED_LABEL)
            except Exception as exc:
                errors.append(exc)
            try:
                self._comment(pull, "bypass", lines=lines)
            except Exception as exc:
                errors.append(exc)
        if errors and not failing:
            raise errors[0]


def run_pass(
    forge: Forge,
    config: Config,
    overlap: Overlap,
    *,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
) -> PassReport:
    """Run one pass, and dispatch the full workflow once when the pass owes it.

    It owes the dispatch when it bypass-merged, or when it found a bypass merge
    that landed after an earlier pass stopped waiting for it.
    """
    run = _Pass(forge, config, overlap, sleep, log)
    try:
        run.run()
    except BaseException:
        run.finish(failing=True)
        raise
    run.finish()
    return run.report
