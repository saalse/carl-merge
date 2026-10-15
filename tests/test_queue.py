"""Locked: one pass over the ready queue, against a fake forge.

Each behaviour-table row has a test that fails if that row's action changes.
The fake implements the `carl_merge.forge.Forge` protocol and nothing else.
"""

import importlib
import importlib.util

import pytest

LABEL = "ready-to-merge"


def _module(name: str):
    full = f"carl_merge.{name}"
    assert importlib.util.find_spec("carl_merge") is not None, (
        "carl_merge does not exist"
    )
    assert importlib.util.find_spec(full) is not None, f"{full} does not exist"
    return importlib.import_module(full)


def _config(dry_run: bool = False):
    return _module("config").parse(
        f'ready_label = "{LABEL}"\nbase_branch = "main"\nci_workflow = "ci.yml"\n'
        f'merge_method = "squash"\ndry_run = {str(dry_run).lower()}\n'
    )


class FakeForge:
    def __init__(self) -> None:
        self.forge = _module("forge")
        self.base = "b0"
        self.red = None
        self.tested = "t0"
        self.open_runs: set[str] = set()
        self.merged_by_sha: dict[str, list[int]] = {}
        self.pulls: dict[int, dict] = {}
        self.checks_by_sha: dict[str, str] = {}
        self.bodies: dict[int, list[str]] = {}
        self.labels: dict[int, set[str]] = {}
        self.events: list[tuple] = []
        self.attempts: list[tuple] = []
        self.merges: list[tuple] = []
        self.updates: list[tuple] = []
        self.cancels: list[int] = []
        self.dispatches: list[tuple] = []
        self.update_conflict: set[int] = set()
        self.move_head_after_checks: set[int] = set()
        self.move_base_on_next_read = False

    def add(
        self,
        number,
        head,
        *,
        behind=False,
        mergeable=True,
        checks="green",
        draft=False,
        fork=False,
    ):
        self.pulls[number] = {
            "head_sha": head,
            "draft": draft,
            "mergeable": mergeable,
            "behind": behind,
            "fork": fork,
            "open": True,
        }
        self.checks_by_sha[head] = checks
        self.labels[number] = {LABEL}
        self.bodies[number] = []

    def ready_pulls(self, label, base):
        self.events.append(("list",))
        return sorted(
            n for n, p in self.pulls.items() if p["open"] and label in self.labels[n]
        )

    def pull(self, number):
        self.events.append(("pull", number))
        p = self.pulls[number]
        return self.forge.Pull(
            number=number,
            head_sha=p["head_sha"],
            head_ref=f"topic-{number}",
            draft=p["draft"],
            mergeable=p["mergeable"],
            behind=p["behind"],
            fork=p["fork"],
        )

    def listed_head(self, number):
        return self.pulls[number]["head_sha"]

    def base_head(self, branch):
        self.events.append(("base",))
        if self.move_base_on_next_read:
            self.move_base_on_next_read = False
            self.base = "b-moved"
        return self.base

    def base_red(self, workflow, branch):
        return self.red

    def base_run_open(self, workflow, branch, sha):
        return sha in self.open_runs

    def merged_pulls(self, sha, since=None):
        return self.merged_by_sha.get(sha, [])

    def checks(self, number, sha):
        self.events.append(("checks", number, sha))
        state = self.checks_by_sha.get(sha, "pending")
        if number in self.move_head_after_checks:
            self.move_head_after_checks.discard(number)
            pushed = f"pushed-{number}"
            self.pulls[number]["head_sha"] = pushed
            self.checks_by_sha[pushed] = "pending"
        return state

    def tested_base(self, number, sha):
        return self.tested

    def comment_bodies(self, number):
        return list(self.bodies[number])

    def comment(self, number, body):
        self.events.append(("comment", number))
        self.bodies[number].append(body)

    def closed_with_label(self, label):
        return []

    def add_label(self, number, label):
        pass

    def remove_label(self, number, label):
        self.events.append(("unlabel", number))
        self.labels[number].discard(label)

    def merge(self, number, sha, method, bypass):
        self.events.append(("merge", number))
        self.attempts.append((number, sha, method, bypass))
        p = self.pulls[number]
        if sha != p["head_sha"]:
            raise self.forge.HeadMoved(f"head is {p['head_sha']}")
        self.merges.append((number, sha, method, bypass))
        p["open"] = False
        self.base = f"m{number}"
        for other in self.pulls.values():
            if other["open"]:
                other["behind"] = True
        return self.base

    def update_branch(self, number, expected_head_sha):
        self.events.append(("update", number))
        self.updates.append((number, expected_head_sha))
        p = self.pulls[number]
        if expected_head_sha != p["head_sha"]:
            raise self.forge.HeadMoved(f"head is {p['head_sha']}")
        if number in self.update_conflict:
            raise self.forge.UpdateConflict("merge conflict")
        p["head_sha"] = f"{expected_head_sha}-u"
        p["behind"] = False
        self.checks_by_sha[p["head_sha"]] = "pending"

    def cancel_runs(self, number, head_ref):
        self.cancels.append(number)
        return 1

    def dispatch(self, workflow, branch):
        self.dispatches.append((workflow, branch))


class Overlap:
    def __init__(self, verdicts, forge=None, move_base=False):
        self.verdicts = verdicts
        self.forge = forge
        self.move_base = move_base
        self.calls: list[tuple] = []

    def __call__(self, pull, base_sha, tested_base):
        self.calls.append((pull.number, base_sha, tested_base))
        if self.move_base:
            self.forge.move_base_on_next_read = True
        verdict = self.verdicts[pull.number]
        if isinstance(verdict, Exception):
            raise verdict
        return _module("overlap").Decision(verdict)


def _run(forge, overlap=None, dry_run=False):
    queue = _module("queue")
    return queue.run_pass(
        forge, _config(dry_run), overlap or Overlap({}), sleep=lambda seconds: None
    )


@pytest.mark.unit
def test_row_1_conflict_is_handed_back():
    forge = FakeForge()
    forge.add(1, "h1", mergeable=False)
    report = _run(forge)
    assert (1, "conflict") in report.actions
    assert LABEL not in forge.labels[1]
    assert forge.bodies[1]
    assert forge.attempts == []


@pytest.mark.unit
def test_row_2_up_to_date_green_merges_pinned():
    forge = FakeForge()
    forge.add(1, "h1")
    report = _run(forge)
    assert forge.merges == [(1, "h1", "squash", False)]
    assert (1, "merge") in report.actions
    assert forge.cancels == [1]
    assert forge.bodies[1]
    assert forge.dispatches == []


@pytest.mark.unit
def test_row_3_behind_disjoint_bypasses_without_update_and_dispatches_once():
    forge = FakeForge()
    forge.add(1, "h1", behind=True)
    forge.add(2, "h2", behind=True)
    report = _run(forge, Overlap({1: "disjoint", 2: "disjoint"}))
    assert forge.merges == [(1, "h1", "squash", True), (2, "h2", "squash", True)]
    assert forge.updates == []
    assert forge.dispatches == [("ci.yml", "main")]
    assert report.dispatched is True


@pytest.mark.unit
def test_row_4_behind_wait_updates_holds_then_merges_when_green():
    forge = FakeForge()
    forge.add(1, "h1", behind=True)
    forge.add(2, "h2", behind=True)
    overlap = Overlap({1: "wait", 2: "disjoint"})
    first = _run(forge, overlap)
    assert forge.updates == [(1, "h1")]
    assert forge.attempts == []
    assert first.stopped == "hold"
    assert any("carl-merge:update:h1-u" in body for body in forge.bodies[1])

    forge.checks_by_sha["h1-u"] = "green"
    second = _run(forge, overlap)
    assert forge.merges == [(1, "h1-u", "squash", False), (2, "h2", "squash", True)]
    assert (1, "merge") in second.actions
    assert forge.dispatches == [("ci.yml", "main")]


@pytest.mark.unit
def test_row_4_conflict_from_the_update_goes_to_row_1():
    forge = FakeForge()
    forge.add(1, "h1", behind=True)
    forge.update_conflict.add(1)
    report = _run(forge, Overlap({1: "wait"}))
    assert (1, "conflict") in report.actions
    assert LABEL not in forge.labels[1]
    assert forge.attempts == []


@pytest.mark.unit
def test_row_5_cannot_decide_comments_once_keeps_the_label_and_moves_on():
    forge = FakeForge()
    forge.add(1, "h1", behind=True)
    forge.add(2, "h2")
    error = _module("gitdiff").OverlapError("git diff failed")
    first = _run(forge, Overlap({1: error}))
    assert (1, "cannot-decide") in first.actions
    assert LABEL in forge.labels[1]
    assert forge.merges == [(2, "h2", "squash", False)]
    _run(forge, Overlap({1: error}))
    assert len(forge.bodies[1]) == 1


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["draft", "fork"])
def test_row_5_draft_or_fork_cannot_be_decided(flag: str):
    forge = FakeForge()
    forge.add(1, "h1", **{flag: True})
    report = _run(forge)
    assert (1, "cannot-decide") in report.actions
    assert LABEL in forge.labels[1]
    assert forge.attempts == []


@pytest.mark.unit
@pytest.mark.parametrize("state", ["red", "unknown"])
def test_row_5_red_or_unknown_required_checks_cannot_be_decided(state: str):
    forge = FakeForge()
    forge.add(1, "h1", behind=True, checks=state)
    report = _run(forge, Overlap({1: "disjoint"}))
    assert (1, "cannot-decide") in report.actions
    assert LABEL in forge.labels[1]
    assert forge.attempts == []
    assert forge.updates == []


@pytest.mark.unit
def test_row_6_red_base_merges_nothing():
    forge = FakeForge()
    forge.red = _module("forge").Run(9, "https://github.com/x/y/runs/9", "b0", "push")
    forge.add(1, "h1")
    forge.add(2, "h2", behind=True)
    report = _run(forge, Overlap({2: "disjoint"}))
    assert report.stopped == "base-red"
    assert forge.attempts == []
    assert forge.updates == []
    assert forge.dispatches == []


@pytest.mark.unit
def test_a_head_that_moves_between_check_and_merge_aborts_the_merge():
    forge = FakeForge()
    forge.add(1, "h1")
    forge.move_head_after_checks.add(1)
    report = _run(forge)
    assert forge.merges == []
    assert (1, "head-moved") in report.actions


@pytest.mark.unit
def test_a_base_that_moves_aborts_a_bypass_merge():
    forge = FakeForge()
    forge.add(1, "h1", behind=True)
    report = _run(forge, Overlap({1: "disjoint"}, forge=forge, move_base=True))
    assert forge.attempts == []
    assert (1, "base-moved") in report.actions


@pytest.mark.unit
def test_two_ready_prs_are_handled_in_turn_against_the_new_base():
    forge = FakeForge()
    forge.add(1, "h1", behind=True)
    forge.add(2, "h2", behind=True)
    overlap = Overlap({1: "disjoint", 2: "disjoint"})
    _run(forge, overlap)
    assert [call[:2] for call in overlap.calls] == [(1, "b0"), (2, "m1")]
    first_merge = forge.events.index(("merge", 1))
    assert ("merge", 2) in forge.events[first_merge + 1 :]
    assert ("pull", 2) in forge.events[first_merge + 1 :]


@pytest.mark.unit
def test_dry_run_comments_what_it_would_do_and_changes_nothing():
    forge = FakeForge()
    forge.add(1, "h1", mergeable=False)
    forge.add(2, "h2")
    forge.add(3, "h3", behind=True)
    forge.add(4, "h4", behind=True)
    _run(forge, Overlap({3: "disjoint", 4: "wait"}), dry_run=True)
    assert forge.attempts == []
    assert forge.updates == []
    assert forge.cancels == []
    assert forge.dispatches == []
    assert all(LABEL in forge.labels[n] for n in (1, 2, 3, 4))
    for n in (1, 2, 3, 4):
        assert any(body.startswith("Dry run") for body in forge.bodies[n]), n


@pytest.mark.unit
def test_unknown_mergeability_is_skipped_without_a_comment():
    forge = FakeForge()
    forge.add(1, "h1", mergeable=None)
    forge.add(2, "h2")
    report = _run(forge)
    assert (1, "skip") in report.actions
    assert forge.bodies[1] == []
    assert forge.merges == [(2, "h2", "squash", False)]


@pytest.mark.unit
def test_pending_checks_hold_only_after_the_bots_own_update():
    forge = FakeForge()
    forge.add(1, "h1", checks="pending")
    forge.add(2, "h2")
    report = _run(forge)
    assert (1, "skip") in report.actions
    assert forge.merges == [(2, "h2", "squash", False)]
