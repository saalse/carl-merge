"""A red base: the opt-in plain-merge lane, the comments, and the dispatch."""

import pytest

from carl_merge import comments
from carl_merge.config import parse
from carl_merge.forge import Pull, Run
from carl_merge.overlap import Decision
from carl_merge.queue import Action, run_pass

LABEL = "ready-to-merge"
RUN_URL = "https://github.com/example/shop/actions/runs/{}"


def config(on_red_base="merge-tested", dry_run=False):
    return parse(
        f'ready_label = "{LABEL}"\nbase_branch = "main"\nci_workflow = "ci.yml"\n'
        f'dry_run = {str(dry_run).lower()}\non_red_base = "{on_red_base}"\n'
    )


def red_run(id=40, sha="b0", event="workflow_dispatch", since=None):
    return Run(id, RUN_URL.format(id), sha, event, since)


class Fake:
    def __init__(self, red=None):
        self.base = "b0"
        self.red = red
        self.red_reads = 0
        self.pulls = {}
        self.checks_by_sha = {}
        self.tested_by_sha = {}
        self.bodies = {}
        self.open_runs = set()
        self.merged_by_sha = {}
        self.merged_in_range = {}
        self.read = []
        self.merges = []
        self.updates = []
        self.dispatches = []

    def add(self, number, head, *, behind=False, checks="green", tested="b0"):
        self.pulls[number] = {"head": head, "behind": behind, "open": True}
        self.checks_by_sha[head] = checks
        self.tested_by_sha[head] = tested
        self.bodies[number] = []

    def ready_pulls(self, label, base):
        return sorted(n for n, p in self.pulls.items() if p["open"])

    def pull(self, number):
        self.read.append(number)
        p = self.pulls[number]
        return Pull(number, p["head"], f"t{number}", False, True, p["behind"], False)

    def listed_head(self, number):
        return self.pulls[number]["head"]

    def base_head(self, branch):
        return self.base

    def base_red(self, workflow, branch):
        self.red_reads += 1
        return self.red

    def base_run_open(self, workflow, branch, sha):
        return sha in self.open_runs

    def merged_pulls(self, sha, since=None):
        if since is not None and (since, sha) in self.merged_in_range:
            return self.merged_in_range[(since, sha)]
        return self.merged_by_sha.get(sha, [])

    def checks(self, number, sha):
        return self.checks_by_sha.get(sha, "pending")

    def tested_base(self, number, sha):
        return self.tested_by_sha.get(sha)

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
        self.merges.append((number, bypass))
        self.pulls[number]["open"] = False
        self.base = f"m{number}"
        for other in self.pulls.values():
            if other["open"]:
                other["behind"] = True
        return self.base

    def update_branch(self, number, expected_head_sha):
        self.updates.append(number)

    def cancel_runs(self, number, head_ref):
        return 0

    def dispatch(self, workflow, branch):
        self.dispatches.append((workflow, branch))


class Overlap:
    def __init__(self):
        self.calls = []

    def __call__(self, pull, base, tested):
        self.calls.append(pull.number)
        return Decision("disjoint")


def run(fake, **kwargs):
    return run_pass(fake, config(**kwargs), Overlap(), sleep=lambda seconds: None)


def marked(fake, number):
    return comments.markers(fake.bodies[number])


# -- the plain-merge lane --------------------------------------------------


@pytest.mark.unit
def test_the_fix_merges_on_a_red_base_when_merge_tested_is_on():
    """The deadlock this lane exists for: the fix is up to date and green."""
    fake = Fake(red=red_run())
    fake.add(1, "h1", behind=True, tested="old")
    fake.add(2, "h2")
    report = run(fake)
    assert fake.merges == [(2, False)]
    assert fake.updates == []
    assert (1, Action.skip) in report.actions
    assert ("base-red", "h1.40") in marked(fake, 1)
    assert ("merge", "h2") in marked(fake, 2)
    assert RUN_URL.format(40) in fake.bodies[2][0]
    assert fake.dispatches == [("ci.yml", "main")]
    assert report.dispatched is True
    assert report.stopped == Action.base_red


@pytest.mark.unit
def test_the_fix_waits_on_a_red_base_by_default():
    fake = Fake(red=red_run())
    fake.add(1, "h1", behind=True, tested="old")
    fake.add(2, "h2")
    report = run_pass(
        fake,
        parse(f'ready_label = "{LABEL}"\nbase_branch = "main"\nci_workflow = "ci.yml"'),
        Overlap(),
        sleep=lambda seconds: None,
    )
    assert fake.merges == []
    assert report.stopped == Action.base_red
    assert report.actions == [(1, Action.skip), (2, Action.skip)]
    assert fake.dispatches == []
    # The default is a dry run, so the comments say what it would do.
    assert ("dry-base-red", "h2.40") in marked(fake, 2)


@pytest.mark.unit
def test_stop_posts_one_base_red_comment_per_head_and_red_run():
    fake = Fake(red=red_run())
    fake.add(1, "h1")
    run(fake, on_red_base="stop")
    run(fake, on_red_base="stop")
    assert len(fake.bodies[1]) == 1
    body = fake.bodies[1][0]
    assert RUN_URL.format(40) in body
    assert body.endswith(comments.marker("base-red", "h1.40"))
    fake.red = red_run(41)
    run(fake, on_red_base="stop")
    fake.pulls[1]["head"] = "h1b"
    run(fake, on_red_base="stop")
    assert marked(fake, 1) == {
        ("base-red", key) for key in ("h1.40", "h1.41", "h1b.41")
    }
    assert len(fake.bodies[1]) == 3
    assert fake.merges == []


@pytest.mark.unit
def test_the_lane_never_bypasses_or_updates_a_behind_pr():
    fake = Fake(red=red_run())
    fake.add(1, "h1", behind=True)
    overlap = Overlap()
    report = run_pass(fake, config(), overlap, sleep=lambda seconds: None)
    assert overlap.calls == []
    assert fake.merges == fake.updates == []
    assert report.actions == [(1, Action.skip)]
    assert "This one cannot: it is behind." in fake.bodies[1][0]
    assert report.stopped is None


@pytest.mark.unit
@pytest.mark.parametrize("tested", ["old", None])
def test_the_lane_skips_a_pr_whose_checks_did_not_run_on_the_base_head(tested):
    fake = Fake(red=red_run())
    fake.add(1, "h1", tested=tested)
    report = run(fake)
    assert fake.merges == []
    assert report.actions == [(1, Action.skip)]
    assert "ran against an older main; rerun them" in fake.bodies[1][0]


@pytest.mark.unit
def test_a_pending_pr_after_its_update_does_not_hold_the_lane():
    fake = Fake(red=red_run())
    fake.add(1, "h1", checks="pending")
    fake.bodies[1].append(
        comments.render("update", head="h0", base="main", new_head="h1")
    )
    fake.add(2, "h2")
    report = run(fake)
    assert (1, Action.skip) in report.actions
    assert fake.merges == [(2, False)]


@pytest.mark.unit
def test_one_merge_on_a_red_base_ends_the_pass():
    fake = Fake(red=red_run())
    fake.add(1, "h1")
    fake.add(2, "h2")
    report = run(fake)
    assert fake.merges == [(1, False)]
    assert 2 not in fake.read
    assert report.stopped == Action.base_red
    assert fake.dispatches == [("ci.yml", "main")]


@pytest.mark.unit
def test_the_lane_reads_the_red_base_once_per_pass():
    fake = Fake(red=red_run())
    fake.add(1, "h1")
    run(fake)
    assert fake.red_reads == 1
    assert fake.merges == [(1, False)]


@pytest.mark.unit
def test_the_lane_in_a_dry_run_merges_and_dispatches_nothing():
    fake = Fake(red=red_run())
    fake.add(1, "h1")
    fake.add(2, "h2")
    report = run(fake, dry_run=True)
    assert fake.merges == fake.dispatches == []
    assert {("dry-merge", "h1"), ("dry-dispatch", "h1")} <= marked(fake, 1)
    assert fake.bodies[2] == []
    assert report.stopped == Action.base_red


@pytest.mark.unit
def test_a_green_base_still_bypasses_as_before_with_merge_tested_on():
    fake = Fake()
    fake.add(1, "h1", behind=True, tested="t0")
    report = run(fake)
    assert fake.merges == [(1, True)]
    assert report.stopped is None


# -- telling the bypass that broke the base --------------------------------


def bypassed(fake, number, merge_sha):
    fake.pulls[number] = {"head": f"h{number}", "behind": False, "open": False}
    fake.bodies[number] = [
        comments.render("bypass", head=f"h{number}", base="main"),
    ]
    fake.merged_by_sha[merge_sha] = [number]


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["stop", "merge-tested"])
def test_the_bypass_whose_full_run_is_red_hears_once_per_run(mode):
    fake = Fake(red=red_run(40, sha="b0"))
    bypassed(fake, 7, "b0")
    run(fake, on_red_base=mode)
    run(fake, on_red_base=mode)
    assert len(fake.bodies[7]) == 2
    body = fake.bodies[7][1]
    assert "after this merge is red: " + RUN_URL.format(40) in body
    assert body.endswith(comments.marker("red-after-bypass", "b0.40"))
    fake.red = red_run(41, sha="b0")
    run(fake, on_red_base=mode)
    assert len(fake.bodies[7]) == 3


@pytest.mark.unit
def test_a_plain_merge_or_a_push_run_names_no_culprit():
    fake = Fake(red=red_run(40, sha="b0"))
    bypassed(fake, 7, "b0")
    fake.bodies[7] = [comments.render("merge", head="h7", base="main")]
    run(fake)
    assert len(fake.bodies[7]) == 1
    fake = Fake(red=red_run(40, sha="b0", event="push"))
    bypassed(fake, 7, "b0")
    run(fake)
    assert len(fake.bodies[7]) == 1


@pytest.mark.unit
def test_every_bypass_the_red_run_tested_hears_not_only_the_last_merge():
    """One pass bypassed #7 then #8 and dispatched once, on #8's merge commit."""
    fake = Fake(red=red_run(40, sha="b0", since="g0"))
    bypassed(fake, 7, "m7")
    bypassed(fake, 8, "b0")
    fake.merged_in_range[("g0", "b0")] = [7, 8]
    run(fake)
    for number in (7, 8):
        assert ("red-after-bypass", "b0.40") in marked(fake, number)


@pytest.mark.unit
def test_a_bypass_before_a_later_plain_merge_still_hears():
    fake = Fake(red=red_run(40, sha="b0", since="g0"))
    bypassed(fake, 7, "m7")
    bypassed(fake, 8, "b0")
    fake.bodies[8] = [comments.render("merge", head="h8", base="main")]
    fake.merged_in_range[("g0", "b0")] = [7, 8]
    run(fake)
    assert ("red-after-bypass", "b0.40") in marked(fake, 7)
    assert len(fake.bodies[8]) == 1


# -- the dispatch on a base that moved past its red run --------------------


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["stop", "merge-tested"])
def test_a_base_moved_past_its_red_run_is_dispatched_once(mode):
    fake = Fake(red=red_run(40, sha="b-old"))
    report = run(fake, on_red_base=mode)
    assert fake.dispatches == [("ci.yml", "main")]
    assert report.dispatched is True
    fake.open_runs.add("b0")
    run(fake, on_red_base=mode)
    assert fake.dispatches == [("ci.yml", "main")]


@pytest.mark.unit
def test_a_run_started_by_the_push_during_the_pass_saves_the_dispatch():
    """The push that moved the base starts CI a moment after it starts Carl."""

    class Late(Fake):
        def base_run_open(self, workflow, branch, sha):
            self.open_reads = getattr(self, "open_reads", 0) + 1
            return self.open_reads > 1

    fake = Late(red=red_run(40, sha="b-old"))
    report = run(fake, on_red_base="stop")
    assert fake.dispatches == []
    assert report.dispatched is False


@pytest.mark.unit
def test_a_failed_look_for_a_run_still_dispatches_what_is_owed():
    class Flaky(Fake):
        def base_run_open(self, workflow, branch, sha):
            self.open_reads = getattr(self, "open_reads", 0) + 1
            if self.open_reads > 1:
                raise RuntimeError("HTTP 502")
            return False

    fake = Flaky(red=red_run(40, sha="b-old"))
    report = run(fake, on_red_base="stop")
    assert fake.dispatches == [("ci.yml", "main")]
    assert report.dispatched is True


@pytest.mark.unit
def test_stop_mode_reads_no_pull_request_beyond_the_listing():
    fake = Fake(red=red_run(40, sha="b0"))
    fake.add(1, "h1")
    fake.add(2, "h2")
    run(fake, on_red_base="stop")
    assert fake.read == []
    assert ("base-red", "h1.40") in marked(fake, 1)
    assert ("base-red", "h2.40") in marked(fake, 2)


@pytest.mark.unit
def test_a_red_run_on_the_base_head_is_not_dispatched_again():
    fake = Fake(red=red_run(40, sha="b0"))
    run(fake)
    assert fake.dispatches == []


@pytest.mark.unit
def test_a_dry_run_never_dispatches_on_a_moved_red_base():
    fake = Fake(red=red_run(40, sha="b-old"))
    run(fake, dry_run=True)
    assert fake.dispatches == []
