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
    Run,
    UpdateConflict,
)
from carl_merge.gitdiff import OverlapError
from carl_merge.overlap import Decision

MERGEABLE_READS = 5
MERGEABLE_WAIT = 2
HEAD_READS = 10
HEAD_WAIT = 3
BASE_RUN_WAIT = 5
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
        self.last_dry_dispatch: Pull | None = None
        # The run that made the base red when the pass began, or None.
        self.red: Run | None = None
        # A run on the base is owed that no bypass label tracks: after a merge
        # on a red base, or when the base moved past its red run. A failed
        # dispatch needs no label, since the next pass finds the base moved.
        self.dispatch_base = False

    # -- records ---------------------------------------------------------

    def _record(self, pull: Pull, action: Action, reason: str = "") -> None:
        self._record_on(pull.number, pull.head_sha, action, reason)

    def _record_on(
        self, number: int, head: str, action: Action, reason: str = ""
    ) -> None:
        self.report.actions.append((number, action))
        first = (reason.splitlines() or [""])[0]
        self.log(f"#{number} {action} {head[:8]} {first}".rstrip())

    def _stop(self, action: Action, reason: str) -> bool:
        self.report.stopped = action
        self.log(f"{action} {reason}")
        return True

    def _comment(self, pull: Pull, action: str, **fields) -> None:
        self._comment_on(pull.number, pull.head_sha, action, **fields)

    def _comment_on(
        self,
        number: int,
        head: str,
        action: str,
        *,
        reason: str = "",
        new_head: str = "",
        workflow: str = "",
        lines: Iterable[str] = (),
        run: Run | None = None,
    ) -> None:
        dry = self.config.dry_run
        run_id = run.id if run else 0
        recorded = comments.markers(self.forge.comment_bodies(number))
        marked = comments.key(action, head, new_head=new_head, run_id=run_id)
        if (("dry-" if dry else "") + action, marked) in recorded:
            return
        body = comments.render(
            action,
            head=head,
            base=self.config.base_branch,
            label=self.config.ready_label,
            dry=dry,
            new_head=new_head,
            reason=reason,
            workflow=workflow or self.config.ci_workflow,
            lines=lines,
            run_id=run_id,
            run_url=run.html_url if run else "",
        )
        self.forge.comment(number, body)

    # -- the pass --------------------------------------------------------

    def run(self) -> None:
        config = self.config
        if not config.dry_run:
            self._settle_owed()
        red = self.red = self.forge.base_red(config.ci_workflow, config.base_branch)
        if red is not None:
            self._red_found(red)
        numbers = self.forge.ready_pulls(config.ready_label, config.base_branch)
        if red is not None and config.on_red_base == "stop":
            for number in numbers:
                # Only the head is needed, and the listing already has it.
                self._wait_on_red(number, self.forge.listed_head(number))
            self._stop(
                Action.base_red, f"the base branch's latest run is red: {red.html_url}"
            )
            return
        if not numbers:
            self.log("queue empty")
            return
        for number in numbers:
            if self._one(number):
                return

    def _red_found(self, red: Run) -> None:
        """Tell the bypass that the red run tested, and test a base that moved.

        A red dispatched run is the full run a pass owed, so it tested every
        merge since the counted run before it; each bypass among them is told.
        A base head past the red run has no verdict of its own until a counted
        run on it completes; when none is queued or running, one is dispatched,
        once, since the next pass sees it running.
        """
        config = self.config
        if red.event == "workflow_dispatch":
            for number in self.forge.merged_pulls(red.head_sha, red.since):
                recorded = comments.markers(self.forge.comment_bodies(number))
                if any(action == "bypass" for action, _ in recorded):
                    self._comment_on(
                        number,
                        red.head_sha,
                        "red-after-bypass",
                        lines=[self._red_policy()],
                        run=red,
                    )
                    self.log(f"#{number} red-after-bypass {red.html_url}")
        base = self.forge.base_head(config.base_branch)
        if base != red.head_sha and not self.forge.base_run_open(
            config.ci_workflow, config.base_branch, base
        ):
            self.dispatch_base = True

    def _red_policy(self) -> str:
        base = self.config.base_branch
        if self.config.on_red_base == "stop":
            return f"I merge nothing into {base} until it is green."
        return (
            f"Until {base} is green, I merge only a PR that is up to date with "
            f"it and whose required checks ran against {base} as it is now."
        )

    def _wait_on_red(self, number: int, head: str, why: str = "") -> None:
        """Skip a PR because the base is red, and say so once per head and run."""
        lines = [self._red_policy(), f"This one cannot: {why}."] if why else []
        self._comment_on(number, head, "base-red", lines=lines, run=self.red)
        reason = f"base is red{', ' + why if why else ''}"
        self._record_on(number, head, Action.skip, reason)

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
            # On a red base the order is not kept, so nothing holds the pass.
            live = self.red is None and ("update", pull.head_sha) in comments.markers(
                self.forge.comment_bodies(number)
            )
            if live:
                self._record(pull, Action.hold, "waiting for its checks after update")
                return self._stop(Action.hold, f"#{number} is next in line")
            self._record(pull, Action.skip, "required checks are pending")
            return False
        base = self.forge.base_head(self.config.base_branch)
        if self.red is not None:
            return self._red_lane(pull, base)
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

    def _red_lane(self, pull: Pull, base: str) -> bool:
        """On a red base, merge only the tree its required checks passed on.

        Never a bypass and never an update: the PR must not be behind, and its
        required checks must have run against the base head itself.
        """
        if pull.behind:
            self._wait_on_red(pull.number, pull.head_sha, "it is behind")
            return False
        if self.forge.tested_base(pull.number, pull.head_sha) != base:
            self._wait_on_red(
                pull.number,
                pull.head_sha,
                f"its required checks ran against an older {self.config.base_branch}; "
                "rerun them",
            )
            return False
        return self._merge(pull, base, bypass=False)

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
        """Re-read before a merge. None means go on; a bool ends this PR.

        A base that turned red during the pass stops it. One that was red when
        the pass began was already weighed by the red lane, so it is not read
        again.
        """
        config = self.config
        if self.red is None and self._turned_red():
            return True
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
        if self.red is not None:
            lines = [
                f"{self.config.base_branch} is red ({self.red.html_url}), but this "
                "PR was up to date and its required checks ran against "
                f"{self.config.base_branch} as it was. {self.config.ci_workflow} "
                f"runs on {self.config.base_branch} after this pass."
            ]
        if self.config.dry_run:
            self._comment(pull, str(action), lines=lines)
            self._record(pull, action, "dry run")
            if bypass or self.red is not None:
                self.last_dry_dispatch = pull
            return self._merged_on_red(pull)
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
        if self.red is not None:
            # Owed before anything else can fail, like a bypass.
            self.dispatch_base = True
        self.forge.cancel_runs(pull.number, pull.head_ref)
        if not bypass:
            self._comment(pull, str(action), lines=lines)
        return self._merged_on_red(pull)

    def _merged_on_red(self, pull: Pull) -> bool:
        """After a merge on a red base, the pass ends; otherwise it goes on.

        The base moved, so no other PR's required checks ran on its head and
        none can take the red lane in this pass. The next pass reads the base
        again.
        """
        if self.red is None:
            return False
        return self._stop(Action.base_red, f"#{pull.number} merged on a red base")

    def _turned_red(self) -> bool:
        config = self.config
        red = self.forge.base_red(config.ci_workflow, config.base_branch)
        if red is None:
            return False
        return self._stop(
            Action.base_red, f"the base branch's latest run is red: {red.html_url}"
        )

    def _update(self, pull: Pull, lines: list[str]) -> bool:
        config = self.config
        # Only a pass that began on a green base updates.
        if self._turned_red():
            return True
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
        if (self.owed or self.dispatch_base) and not config.dry_run:
            if not self.owed and not failing and self._base_run_started():
                self.log("no dispatch: a counted run is already on the base head")
                return
            try:
                self.forge.dispatch(config.ci_workflow, config.base_branch)
            except Exception:
                if not failing:
                    raise
                return
            self.report.dispatched = True
            self.log(f"dispatch {config.ci_workflow} {config.base_branch}")
            self._settle_dispatched(failing)
        elif self.last_dry_dispatch is not None and not failing:
            self._comment(
                self.last_dry_dispatch, "dispatch", workflow=config.ci_workflow
            )

    def _base_run_started(self) -> bool:
        """Has a counted run started on the base head since the pass read it?

        The push that moved the base usually starts its own run, a little
        after it starts this pass. A bypass is still dispatched regardless,
        since its owed run must be the full one, and so is the base when the
        look itself fails.
        """
        config = self.config
        self.sleep(BASE_RUN_WAIT)
        try:
            head = self.forge.base_head(config.base_branch)
            return self.forge.base_run_open(
                config.ci_workflow, config.base_branch, head
            )
        except Exception as exc:
            # A failed look must not cost the run that is owed.
            self.log(f"dispatching anyway: the look for a run failed: {exc}")
            return False

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

    It owes the dispatch when it bypass-merged, when it found a bypass merge
    that landed after an earlier pass stopped waiting for it, when it merged on
    a red base, or when the base head moved past its red run and no counted run
    is queued or running on it.
    """
    run = _Pass(forge, config, overlap, sleep, log)
    try:
        run.run()
    except BaseException:
        run.finish(failing=True)
        raise
    run.finish()
    return run.report
