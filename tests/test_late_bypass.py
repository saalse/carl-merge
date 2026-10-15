"""Locked: a bypass merge that outlasts the polls still gets its one dispatch.

The merge request can finish after the pass that sent it has stopped polling.
A later pass that finds the merge done dispatches the configured workflow on
the base branch once, and only once the merge commit exists. A dry run never
dispatches. The fake implements the `carl_merge.forge.Forge` protocol and
nothing else.
"""

import dataclasses

import pytest

from carl_merge.config import parse
from carl_merge.forge import MergeTimeout, Pull
from carl_merge.overlap import Decision
from carl_merge.queue import run_pass

LABEL = "ready-to-merge"
CI = ("ci.yml", "main")


def _config(dry_run=False):
    return parse(
        f'ready_label = "{LABEL}"\nbase_branch = "main"\nci_workflow = "ci.yml"\n'
        f"dry_run = {str(dry_run).lower()}\n"
    )


def _pull(**fields):
    known = {f.name for f in dataclasses.fields(Pull)}
    return Pull(**{name: value for name, value in fields.items() if name in known})


class Fake:
    def __init__(self):
        self.base = "b0"
        self.pulls = {}
        self.labels = {}
        self.bodies = {}
        self.slow = set()
        self.in_flight = {}
        self.merges = []
        self.dispatches = []
        self.merged_at_dispatch = []

    def add(self, number, head, *, behind=True, slow=False):
        self.pulls[number] = {
            "head": head,
            "behind": behind,
            "open": True,
            "merged": False,
        }
        self.labels[number] = {LABEL}
        self.bodies[number] = []
        if slow:
            self.slow.add(number)

    def land(self, number):
        """GitHub finishes a merge the bot stopped waiting for."""
        assert number in self.in_flight, f"no merge of {number} was requested"
        self._merged(number, self.in_flight.pop(number))

    def _merged(self, number, bypass):
        p = self.pulls[number]
        p["open"], p["merged"] = False, True
        self.merges.append((number, bypass))
        self.base = f"m{number}"
        for other in self.pulls.values():
            if other["open"]:
                other["behind"] = True

    # -- the Forge protocol ----------------------------------------------

    def ready_pulls(self, label, base):
        return sorted(
            n for n, p in self.pulls.items() if p["open"] and label in self.labels[n]
        )

    def pull(self, number):
        p = self.pulls[number]
        return _pull(
            number=number,
            head_sha=p["head"],
            head_ref=f"topic-{number}",
            draft=False,
            mergeable=True,
            behind=p["behind"],
            fork=False,
            merged=p["merged"],
        )

    def base_head(self, branch):
        return self.base

    def base_red(self, workflow, branch):
        return False

    def checks(self, number, sha):
        return "green"

    def tested_base(self, number, sha):
        return "t0"

    def comment_bodies(self, number):
        return list(self.bodies[number])

    def comment(self, number, body):
        self.bodies[number].append(body)

    def closed_with_label(self, label):
        return sorted(
            n
            for n, p in self.pulls.items()
            if not p["open"] and label in self.labels[n]
        )

    def add_label(self, number, label):
        self.labels[number].add(label)

    def remove_label(self, number, label):
        self.labels[number].discard(label)

    def merge(self, number, sha, method, bypass):
        assert sha == self.pulls[number]["head"]
        if number in self.slow:
            self.in_flight[number] = bypass
            raise MergeTimeout("the merge had not finished after the polls")
        self._merged(number, bypass)
        return self.base

    def update_branch(self, number, expected_head_sha):
        raise AssertionError("no update is expected in these tests")

    def cancel_runs(self, number, head_ref):
        return 0

    def dispatch(self, workflow, branch):
        self.dispatches.append((workflow, branch))
        self.merged_at_dispatch.append(
            sorted(n for n, p in self.pulls.items() if p["merged"])
        )


def _pass(fake, *, dry_run=False):
    return run_pass(
        fake,
        _config(dry_run),
        lambda pull, base, tested: Decision("disjoint"),
        sleep=lambda seconds: None,
    )


@pytest.mark.unit
def test_a_bypass_that_lands_after_the_polls_is_dispatched_once_after_it_exists():
    fake = Fake()
    fake.add(1, "h1", slow=True)

    first = _pass(fake)
    assert first.stopped == "hold"
    assert fake.dispatches == []

    still_running = _pass(fake)
    assert fake.dispatches == [], "nothing is dispatched before the merge lands"
    assert still_running.dispatched is False

    fake.land(1)
    landed = _pass(fake)
    assert fake.dispatches == [CI]
    assert fake.merged_at_dispatch == [[1]], "dispatched before the merge existed"
    assert landed.dispatched is True
    assert any("carl-merge:bypass:h1" in body for body in fake.bodies[1])

    _pass(fake)
    _pass(fake)
    assert fake.dispatches == [CI], "a landed bypass is dispatched only once"


@pytest.mark.unit
def test_one_dispatch_per_pass_whether_its_bypass_landed_in_it_or_later():
    fake = Fake()
    fake.add(1, "h1")
    fake.add(2, "h2", slow=True)

    first = _pass(fake)
    assert fake.merges == [(1, True)]
    assert first.stopped == "hold"
    assert fake.dispatches == [CI], "a bypass within the polls dispatches once"

    fake.land(2)
    _pass(fake)
    assert fake.dispatches == [CI, CI], "the late bypass gets its own dispatch"

    fake.add(3, "h3")
    _pass(fake)
    assert fake.merges[-1] == (3, True)
    assert fake.dispatches == [CI, CI, CI]

    fake.add(4, "h4", slow=True)
    _pass(fake)
    fake.land(4)
    fake.add(5, "h5")
    both = _pass(fake)
    assert fake.merges[-2:] == [(4, True), (5, True)]
    assert both.dispatched is True
    assert fake.dispatches == [CI] * 4, "one dispatch covers both in one pass"

    _pass(fake)
    assert fake.dispatches == [CI] * 4


@pytest.mark.unit
def test_a_dry_run_never_dispatches_and_leaves_the_owed_run_for_a_live_pass():
    fake = Fake()
    fake.add(1, "h1", slow=True)
    _pass(fake)
    fake.land(1)

    dry = _pass(fake, dry_run=True)
    assert fake.dispatches == []
    assert dry.dispatched is False

    _pass(fake)
    assert fake.dispatches == [CI]
