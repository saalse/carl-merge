"""Queue edges the locked tests leave open: errors, holds and the dispatch rule."""

import dataclasses

import pytest

from carl_merge import comments
from carl_merge.config import parse
from carl_merge.forge import (
    ForgeError,
    HeadMoved,
    MergeRefused,
    MergeTimeout,
    Pull,
)
from carl_merge.overlap import Decision
from carl_merge.queue import OWED_LABEL, Action, run_pass

LABEL = "ready-to-merge"


def config(dry_run=False):
    return parse(
        f'ready_label = "{LABEL}"\nbase_branch = "main"\nci_workflow = "ci.yml"\n'
        f"dry_run = {str(dry_run).lower()}\n"
    )


class Fake:
    def __init__(self):
        self.base = "b0"
        self.tested = "t0"
        self.red_after_merges = None
        self.pulls = {}
        self.checks_by_sha = {}
        self.bodies = {}
        self.merge_error = {}
        self.pull_error = set()
        self.static_head = False
        self.merges = []
        self.updates = []
        self.dispatches = []

    def add(self, number, head, *, behind=True, checks="green"):
        self.pulls[number] = {"head": head, "behind": behind, "open": True}
        self.checks_by_sha[head] = checks
        self.bodies[number] = []

    def ready_pulls(self, label, base):
        return sorted(n for n, p in self.pulls.items() if p["open"])

    def pull(self, number):
        if number in self.pull_error:
            raise ForgeError("GET /pulls: HTTP 502", 502)
        p = self.pulls[number]
        return Pull(number, p["head"], f"t{number}", False, True, p["behind"], False)

    def base_head(self, branch):
        return self.base

    def base_red(self, workflow, branch):
        return self.red_after_merges is not None and len(self.merges) >= (
            self.red_after_merges
        )

    def checks(self, number, sha):
        return self.checks_by_sha.get(sha, "pending")

    def tested_base(self, number, sha):
        return self.tested

    def comment_bodies(self, number):
        return list(self.bodies[number])

    def comment(self, number, body):
        self.bodies[number].append(body)

    def closed_with_label(self, label):
        return []

    def add_label(self, number, label):
        pass

    def remove_label(self, number, label):
        pass

    def merge(self, number, sha, method, bypass):
        if number in self.merge_error:
            raise self.merge_error[number]
        self.merges.append((number, bypass))
        self.pulls[number]["open"] = False
        self.base = f"m{number}"
        return self.base

    def update_branch(self, number, expected_head_sha):
        self.updates.append(number)
        if not self.static_head:
            self.pulls[number]["head"] = expected_head_sha + "-u"

    def cancel_runs(self, number, head_ref):
        return 0

    def dispatch(self, workflow, branch):
        self.dispatches.append((workflow, branch))


def overlap(verdicts):
    return lambda pull, base, tested: Decision(verdicts[pull.number])


def run(fake, verdicts, **kwargs):
    return run_pass(
        fake, config(**kwargs), overlap(verdicts), sleep=lambda seconds: None
    )


@pytest.mark.unit
def test_a_forge_error_after_a_bypass_still_dispatches_once():
    fake = Fake()
    fake.add(1, "h1")
    fake.add(2, "h2")
    fake.pull_error.add(2)
    with pytest.raises(ForgeError):
        run(fake, {1: "disjoint", 2: "disjoint"})
    assert fake.merges == [(1, True)]
    assert fake.dispatches == [("ci.yml", "main")]


@pytest.mark.unit
def test_a_hold_after_a_bypass_dispatches_once():
    fake = Fake()
    fake.add(1, "h1")
    fake.add(2, "h2")
    report = run(fake, {1: "disjoint", 2: "wait"})
    assert report.stopped == Action.hold
    assert fake.updates == [2]
    assert fake.dispatches == [("ci.yml", "main")]


@pytest.mark.unit
def test_a_normal_merge_never_dispatches():
    fake = Fake()
    fake.add(1, "h1", behind=False)
    run(fake, {})
    assert fake.merges == [(1, False)]
    assert fake.dispatches == []


@pytest.mark.unit
def test_a_red_base_after_a_merge_stops_the_pass_and_dispatches():
    fake = Fake()
    fake.add(1, "h1")
    fake.add(2, "h2")
    fake.red_after_merges = 1
    report = run(fake, {1: "disjoint", 2: "disjoint"})
    assert report.stopped == Action.base_red
    assert fake.merges == [(1, True)]
    assert fake.dispatches == [("ci.yml", "main")]


@pytest.mark.unit
def test_a_refused_merge_is_cannot_decide_with_the_message():
    fake = Fake()
    fake.add(1, "h1", behind=False)
    fake.merge_error[1] = MergeRefused("blocked by a rule")
    report = run(fake, {})
    assert (1, "cannot-decide") in report.actions
    assert "blocked by a rule" in fake.bodies[1][0]


@pytest.mark.unit
def test_a_head_that_moves_inside_the_merge_is_head_moved():
    fake = Fake()
    fake.add(1, "h1", behind=False)
    fake.merge_error[1] = HeadMoved("moved")
    report = run(fake, {})
    assert (1, "head-moved") in report.actions
    assert fake.bodies[1] == []
    assert fake.dispatches == []


@pytest.mark.unit
def test_a_merge_that_times_out_holds_the_pass():
    fake = Fake()
    fake.add(1, "h1", behind=False)
    fake.add(2, "h2", behind=False)
    fake.merge_error[1] = MergeTimeout("slow")
    report = run(fake, {})
    assert report.stopped == Action.hold
    assert fake.merges == []


@pytest.mark.unit
def test_an_update_that_never_changes_the_head_holds_without_a_comment():
    fake = Fake()
    fake.add(1, "h1")
    fake.add(2, "h2")
    fake.static_head = True
    report = run(fake, {1: "wait", 2: "disjoint"})
    assert report.stopped == Action.hold
    assert fake.bodies[1] == []
    assert fake.merges == []


@pytest.mark.unit
def test_only_a_live_update_marker_holds_a_pending_head():
    fake = Fake()
    fake.add(1, "h1", behind=False, checks="pending")
    fake.add(2, "h2", behind=False)
    fake.bodies[1] = [comments.marker("update", "h1", dry=True)]
    report = run(fake, {})
    assert (1, "skip") in report.actions
    assert fake.merges == [(2, False)]

    fake.bodies[1] = [comments.marker("update", "h1")]
    report = run(fake, {})
    assert report.stopped == Action.hold
    assert fake.merges == [(2, False)]


@pytest.mark.unit
def test_dry_run_notes_the_dispatch_it_would_make():
    fake = Fake()
    fake.add(1, "h1")
    report = run(fake, {1: "disjoint"}, dry_run=True)
    assert fake.dispatches == []
    assert report.dispatched is False
    assert any("would run ci.yml" in body for body in fake.bodies[1])


@pytest.mark.unit
def test_an_empty_queue_logs_and_does_nothing():
    lines = []
    report = run_pass(Fake(), config(), overlap({}), log=lines.append)
    assert lines == ["queue empty"]
    assert report.actions == []
    assert report.stopped is None


@pytest.mark.unit
def test_every_action_logs_one_line_with_the_short_head():
    fake = Fake()
    fake.add(7, "abcdef0123456789", behind=False)
    lines = []
    run_pass(fake, config(), overlap({}), log=lines.append, sleep=lambda s: None)
    assert lines == ["#7 merge abcdef01"]


@pytest.mark.unit
def test_a_behind_pr_with_no_known_tested_base_is_updated_not_bypassed():
    fake = Fake()
    fake.add(1, "h1")
    fake.tested = None
    calls = []

    def overlap_never(pull, base, tested):
        calls.append(pull.number)
        return Decision("disjoint")

    report = run_pass(fake, config(), overlap_never, sleep=lambda s: None)
    assert calls == []
    assert fake.merges == []
    assert fake.updates == [1]
    assert (1, "update") in report.actions
    assert report.stopped == Action.hold
    assert "cannot be tied to one tested base" in fake.bodies[1][0]


class Owing(Fake):
    """The edge fake, with labels and `merged` for the owed-dispatch rows."""

    def __init__(self):
        super().__init__()
        self.labels = {}
        self.merged = set()
        self.dispatch_error = None
        self.label_error = None
        self.comment_error = None
        self.unlabel_error = set()
        self.cancel_error = set()
        self.owed_at_merge = []

    def owe(self, number, head, *, merged=True, bodies=()):
        """A closed pull request that already carries the owed label."""
        self.add(number, head)
        self.pulls[number]["open"] = False
        if merged:
            self.merged.add(number)
        self.labels[number] = {OWED_LABEL}
        self.bodies[number] = list(bodies)

    def pull(self, number):
        return dataclasses.replace(super().pull(number), merged=number in self.merged)

    def merge(self, number, sha, method, bypass):
        self.owed_at_merge.append(OWED_LABEL in self.labels.get(number, set()))
        base = super().merge(number, sha, method, bypass)
        self.merged.add(number)
        return base

    def closed_with_label(self, label):
        return sorted(
            n
            for n, p in self.pulls.items()
            if not p["open"] and label in self.labels.get(n, set())
        )

    def cancel_runs(self, number, head_ref):
        if number in self.cancel_error:
            raise ForgeError("POST cancel: HTTP 502", 502)
        return 0

    def add_label(self, number, label):
        if self.label_error is not None:
            raise self.label_error
        self.labels.setdefault(number, set()).add(label)

    def comment(self, number, body):
        if self.comment_error is not None:
            raise self.comment_error
        super().comment(number, body)

    def remove_label(self, number, label):
        if number in self.unlabel_error:
            raise ForgeError("DELETE labels: HTTP 502", 502)
        self.labels.get(number, set()).discard(label)

    def dispatch(self, workflow, branch):
        if self.dispatch_error is not None:
            raise self.dispatch_error
        super().dispatch(workflow, branch)


@pytest.mark.unit
def test_a_plain_merge_that_times_out_owes_nothing_when_it_lands():
    fake = Owing()
    fake.add(1, "h1", behind=False)
    fake.merge_error[1] = MergeTimeout("slow")
    report = run(fake, {})
    assert report.stopped == Action.hold
    assert OWED_LABEL not in fake.labels.get(1, set())

    fake.pulls[1]["open"] = False
    fake.merged.add(1)
    report = run(fake, {})
    assert fake.dispatches == []
    assert (1, "landed") not in report.actions


@pytest.mark.unit
def test_a_labelled_pr_closed_unmerged_is_unlabelled_without_a_dispatch():
    fake = Owing()
    fake.owe(1, "h1", merged=False)
    report = run(fake, {})
    assert OWED_LABEL not in fake.labels[1]
    assert fake.dispatches == []
    assert report.dispatched is False
    assert fake.bodies[1] == []


@pytest.mark.unit
@pytest.mark.parametrize("action", ["merge", "bypass"])
def test_a_labelled_pr_already_marked_is_unlabelled_without_a_dispatch(action):
    fake = Owing()
    fake.owe(1, "h1", bodies=[comments.marker(action, "h1")])
    report = run(fake, {})
    assert OWED_LABEL not in fake.labels[1]
    assert fake.dispatches == []
    assert (1, "landed") not in report.actions


@pytest.mark.unit
def test_a_failed_owed_dispatch_keeps_the_label_and_the_next_pass_retries_once():
    fake = Owing()
    fake.owe(1, "h1")
    fake.dispatch_error = ForgeError("POST dispatches: HTTP 502", 502)
    with pytest.raises(ForgeError):
        run(fake, {})
    assert OWED_LABEL in fake.labels[1]
    assert fake.bodies[1] == []

    fake.dispatch_error = None
    lines = []
    report = run_pass(fake, config(), overlap({}), log=lines.append)
    assert report.dispatched is True
    assert fake.dispatches == [("ci.yml", "main")]
    assert OWED_LABEL not in fake.labels[1]
    assert any(comments.marker("bypass", "h1") in body for body in fake.bodies[1])
    assert lines[0].startswith("#1 landed h1")

    run(fake, {})
    assert fake.dispatches == [("ci.yml", "main")]


@pytest.mark.unit
def test_a_forge_error_after_an_owed_settle_still_dispatches_once():
    fake = Owing()
    fake.owe(1, "h1")
    fake.add(2, "h2")
    fake.pull_error.add(2)
    with pytest.raises(ForgeError):
        run(fake, {2: "disjoint"})
    assert fake.dispatches == [("ci.yml", "main")]
    assert OWED_LABEL not in fake.labels[1]


@pytest.mark.unit
def test_a_bypass_is_owed_while_its_merge_runs_and_cleared_when_it_lands():
    fake = Owing()
    fake.add(1, "h1")
    report = run(fake, {1: "disjoint"})
    assert fake.owed_at_merge == [True]
    assert OWED_LABEL not in fake.labels[1]
    assert fake.dispatches == [("ci.yml", "main")]
    assert report.dispatched is True

    run(fake, {})
    assert fake.dispatches == [("ci.yml", "main")]


@pytest.mark.unit
def test_a_bypass_whose_label_cannot_be_added_is_not_sent():
    fake = Owing()
    fake.add(1, "h1")
    fake.label_error = ForgeError("POST labels: HTTP 502", 502)
    with pytest.raises(ForgeError):
        run(fake, {1: "disjoint"})
    assert fake.merges == []
    assert fake.dispatches == []


@pytest.mark.unit
def test_a_refused_bypass_keeps_the_label_until_the_pr_closes_unmerged():
    fake = Owing()
    fake.add(1, "h1")
    fake.merge_error[1] = MergeRefused("blocked by a rule")
    report = run(fake, {1: "disjoint"})
    assert (1, "cannot-decide") in report.actions
    assert OWED_LABEL in fake.labels[1]

    fake.pulls[1]["open"] = False
    run(fake, {})
    assert OWED_LABEL not in fake.labels[1]
    assert fake.dispatches == []


@pytest.mark.unit
def test_a_comment_that_cannot_be_posted_does_not_repeat_the_owed_dispatch():
    fake = Owing()
    fake.owe(1, "h1")
    fake.comment_error = ForgeError("POST comments: HTTP 422", 422)
    with pytest.raises(ForgeError):
        run(fake, {})
    assert fake.dispatches == [("ci.yml", "main")]
    assert OWED_LABEL not in fake.labels[1]

    run(fake, {})
    assert fake.dispatches == [("ci.yml", "main")]


@pytest.mark.unit
def test_a_failed_dispatch_after_an_in_time_bypass_is_retried_by_the_next_pass():
    fake = Owing()
    fake.add(1, "h1")
    fake.dispatch_error = ForgeError("POST dispatches: HTTP 502", 502)
    with pytest.raises(ForgeError):
        run(fake, {1: "disjoint"})
    assert fake.merges == [(1, True)]
    assert OWED_LABEL in fake.labels[1]
    assert fake.bodies[1] == []

    fake.dispatch_error = None
    report = run(fake, {})
    assert report.dispatched is True
    assert fake.dispatches == [("ci.yml", "main")]
    assert OWED_LABEL not in fake.labels[1]
    assert any(comments.marker("bypass", "h1") in body for body in fake.bodies[1])

    run(fake, {})
    assert fake.dispatches == [("ci.yml", "main")]


@pytest.mark.unit
def test_a_label_that_cannot_be_removed_still_gets_its_marker_and_no_new_dispatch():
    fake = Owing()
    fake.owe(1, "h1")
    fake.owe(2, "h2")
    fake.unlabel_error.add(1)
    with pytest.raises(ForgeError):
        run(fake, {})
    assert fake.dispatches == [("ci.yml", "main")]
    assert any(comments.marker("bypass", "h1") in body for body in fake.bodies[1])
    assert OWED_LABEL not in fake.labels[2]
    assert any(comments.marker("bypass", "h2") in body for body in fake.bodies[2])

    fake.unlabel_error.clear()
    report = run(fake, {})
    assert fake.dispatches == [("ci.yml", "main")]
    assert report.dispatched is False
    assert OWED_LABEL not in fake.labels[1]


@pytest.mark.unit
@pytest.mark.parametrize(
    "failing, landed, dispatches",
    [
        # The pass stops at #1; the next one bypasses #2 and dispatches for it.
        (1, [1], 2),
        # Both landed before the error; the one dispatch covers both.
        (2, [1, 2], 1),
    ],
)
def test_a_bypass_whose_run_cancel_fails_is_still_owed_by_this_pass(
    failing, landed, dispatches
):
    fake = Owing()
    fake.add(1, "h1")
    fake.add(2, "h2")
    fake.cancel_error = {failing}
    with pytest.raises(ForgeError):
        run(fake, {1: "disjoint", 2: "disjoint"})
    assert [number for number, _ in fake.merges] == landed
    assert fake.dispatches == [("ci.yml", "main")]
    for number in landed:
        assert OWED_LABEL not in fake.labels[number]
        marker = comments.marker("bypass", f"h{number}")
        assert any(marker in body for body in fake.bodies[number])

    fake.cancel_error = set()
    run(fake, {1: "disjoint", 2: "disjoint"})
    assert fake.dispatches == [("ci.yml", "main")] * dispatches
