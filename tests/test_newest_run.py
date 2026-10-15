"""Only the newest run of a required check decides `checks()` and `tested_base()`.

A pull request that went from draft to ready carries two runs of one check in
two check suites: the draft one failed, the ready one passed. The forge counts
the newest run of each required name, so the bot must too.
"""

import pytest

from carl_merge import rest
from carl_merge.transport import Response

REPO = "/repos/example/shop"
SHA = "a" * 40
CHECKS = f"{REPO}/commits/{SHA}/check-runs"
STATUS = f"{REPO}/commits/{SHA}/status"
RUNS = f"{REPO}/actions/runs"


class Wire:
    """A fake wire that answers the three reads these methods make."""

    def __init__(self, check_runs, statuses=(), workflow_runs=()):
        self.replies = {
            CHECKS: list(check_runs),
            RUNS: list(workflow_runs),
        }
        self.statuses = list(statuses)

    def pages(self, path, *, params=None, key=None):
        return self.replies[path]

    def request(self, method, path, *, params=None, body=None, expect=(200,)):
        assert (method, path) == ("GET", STATUS)
        return Response(200, {"statuses": self.statuses})


def forge(wire, *names):
    return rest.RestForge(
        wire,
        repo="example/shop",
        app_slug="carl",
        base_branch="main",
        required_checks=names,
        sleep=lambda seconds: None,
    )


def run(id, name, suite, conclusion="success", status="completed", app=1):
    return {
        "id": id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "check_suite": {"id": suite},
        "app": {"id": app},
    }


def owning(suite, base, number=7):
    return {
        "check_suite_id": suite,
        "pull_requests": [{"number": number, "base": {"sha": base}}],
    }


@pytest.mark.unit
@pytest.mark.parametrize("newest_first", [False, True])
def test_a_newer_pass_supersedes_an_older_failure(newest_first):
    older = run(10, "lint", suite=1, conclusion="failure")
    newer = run(20, "lint", suite=2)
    listed = [newer, older] if newest_first else [older, newer]
    assert forge(Wire(listed), "lint").checks(7, SHA) == "green"


@pytest.mark.unit
@pytest.mark.parametrize("newest_first", [False, True])
def test_a_newer_failure_supersedes_an_older_pass(newest_first):
    older = run(10, "lint", suite=1)
    newer = run(20, "lint", suite=2, conclusion="failure")
    listed = [newer, older] if newest_first else [older, newer]
    assert forge(Wire(listed), "lint").checks(7, SHA) == "red"


@pytest.mark.unit
def test_a_newer_unfinished_run_reads_pending_over_an_older_failure():
    older = run(10, "lint", suite=1, conclusion="failure")
    newer = run(20, "lint", suite=2, status="queued", conclusion=None)
    assert forge(Wire([older, newer]), "lint").checks(7, SHA) == "pending"


@pytest.mark.unit
def test_a_newer_skipped_run_supersedes_an_older_failure():
    older = run(10, "lint", suite=1, conclusion="failure")
    newer = run(20, "lint", suite=2, conclusion="skipped")
    assert forge(Wire([older, newer]), "lint").checks(7, SHA) == "green"


@pytest.mark.unit
def test_each_required_name_is_read_from_its_own_newest_run():
    runs = [
        run(10, "lint", suite=1, conclusion="failure"),
        run(20, "lint", suite=2),
        run(21, "tests", suite=2, conclusion="failure"),
    ]
    assert forge(Wire(runs), "lint", "tests").checks(7, SHA) == "red"


@pytest.mark.unit
def test_a_newer_run_from_another_app_does_not_supersede_a_pinned_one():
    pinned_rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [{"context": "lint", "integration_id": 222}]
            },
        }
    ]

    class Pinned(Wire):
        def pages(self, path, *, params=None, key=None):
            if path.endswith("/rules/branches/main"):
                return pinned_rules
            return super().pages(path, params=params, key=key)

    older = run(10, "lint", suite=1, conclusion="failure", app=222)
    newer = run(20, "lint", suite=2, app=111)
    wire = Pinned([older, newer])
    pinned = rest.RestForge(
        wire,
        repo="example/shop",
        app_slug="carl",
        base_branch="main",
        sleep=lambda seconds: None,
    )
    assert pinned.checks(7, SHA) == "red"


@pytest.mark.unit
def test_a_failing_commit_status_of_the_same_name_still_counts():
    wire = Wire(
        [run(20, "lint", suite=2)],
        statuses=[{"context": "lint", "state": "failure"}],
    )
    assert forge(wire, "lint").checks(7, SHA) == "red"


@pytest.mark.unit
def test_the_tested_base_is_the_newest_runs_not_the_superseded_runs():
    runs = [run(10, "lint", suite=1), run(20, "lint", suite=2)]
    owners = [owning(1, "d" * 40), owning(2, "r" * 40)]
    assert forge(Wire(runs, workflow_runs=owners), "lint").tested_base(7, SHA) == (
        "r" * 40
    )


@pytest.mark.unit
def test_a_superseded_suite_with_no_owning_run_does_not_hide_the_tested_base():
    runs = [
        run(10, "lint", suite=1, conclusion="failure"),
        run(20, "lint", suite=3),
        run(21, "tests", suite=3),
    ]
    owners = [owning(3, "r" * 40)]
    wire = Wire(runs, workflow_runs=owners)
    assert forge(wire, "lint", "tests").tested_base(7, SHA) == "r" * 40


@pytest.mark.unit
def test_two_newest_runs_on_different_bases_still_have_no_tested_base():
    runs = [run(20, "lint", suite=2), run(21, "tests", suite=3)]
    owners = [owning(2, "a" * 40), owning(3, "b" * 40)]
    assert (
        forge(Wire(runs, workflow_runs=owners), "lint", "tests").tested_base(7, SHA)
        is None
    )
