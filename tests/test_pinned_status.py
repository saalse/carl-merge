"""A required check pinned to an app is met by a commit status from that app.

A ruleset can pin a required check to an app (`integration_id`). The forge then
counts a check run or a commit status, either of which that app made. The
combined status names no creator, so a pinned requirement reads the full status
list, and turns each bot creator into its app with `GET /apps/{slug}`. A status
of that name from anyone else never meets the requirement.
"""

import pytest

from carl_merge import rest
from carl_merge.forge import ForgeError
from carl_merge.transport import Response

REPO = "/repos/example/shop"
SHA = "a" * 40
CHECKS = f"{REPO}/commits/{SHA}/check-runs"
STATUS = f"{REPO}/commits/{SHA}/status"
STATUSES = f"{REPO}/commits/{SHA}/statuses"
RULES = f"{REPO}/rules/branches/main"
APPS = {"ci-runner": 222, "other-bot": 111}


def rules(*needs):
    return [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": name, **({"integration_id": app} if app else {})}
                    for name, app in needs
                ]
            },
        }
    ]


class Wire:
    """A fake wire for the reads `checks()` makes, counting app lookups."""

    def __init__(self, needs, statuses=(), check_runs=(), app_reply=None):
        self.replies = {
            RULES: rules(*needs),
            CHECKS: list(check_runs),
            STATUSES: list(statuses),
        }
        self.app_reply = app_reply
        self.app_lookups: list[str] = []

    def pages(self, path, *, params=None, key=None):
        return self.replies[path]

    def request(self, method, path, *, params=None, body=None, expect=(200,)):
        assert method == "GET"
        if path == STATUS:
            return Response(200, {"statuses": []})
        assert path.startswith("/apps/"), path
        slug = path.removeprefix("/apps/")
        self.app_lookups.append(slug)
        if self.app_reply is not None:
            reply = self.app_reply
        elif slug in APPS:
            reply = Response(200, {"id": APPS[slug], "slug": slug})
        else:
            reply = Response(404, {"message": "Not Found"})
        if reply.status not in expect:
            raise ForgeError(f"GET {path}: HTTP {reply.status}", reply.status)
        return reply


def forge(wire):
    return rest.RestForge(
        wire,
        repo="example/shop",
        app_slug="carl",
        base_branch="main",
        sleep=lambda seconds: None,
    )


def status(id, context, state, login="ci-runner[bot]", type="Bot"):
    return {
        "id": id,
        "context": context,
        "state": state,
        "creator": {"login": login, "type": type},
    }


def run(id, name, conclusion="success", app=222):
    return {
        "id": id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "check_suite": {"id": 1},
        "app": {"id": app},
    }


@pytest.mark.unit
def test_a_successful_status_from_the_pinned_app_meets_the_requirement():
    wire = Wire([("gate", 222)], statuses=[status(1, "gate", "success")])
    assert forge(wire).checks(7, SHA) == "green"


@pytest.mark.unit
@pytest.mark.parametrize(
    "login, type",
    [("other-bot[bot]", "Bot"), ("unknown-bot[bot]", "Bot"), ("octocat", "User")],
)
def test_the_same_status_from_anyone_else_leaves_it_pending(login, type):
    wire = Wire([("gate", 222)], statuses=[status(1, "gate", "success", login, type)])
    assert forge(wire).checks(7, SHA) == "pending"


@pytest.mark.unit
@pytest.mark.parametrize(
    "state, expected",
    [("pending", "pending"), ("failure", "red"), ("error", "red")],
)
def test_a_pending_or_failing_status_from_the_pinned_app_reads_so(state, expected):
    wire = Wire([("gate", 222)], statuses=[status(1, "gate", state)])
    assert forge(wire).checks(7, SHA) == expected


@pytest.mark.unit
@pytest.mark.parametrize("newest_first", [False, True])
def test_only_the_newest_status_from_the_pinned_app_decides(newest_first):
    older = status(1, "gate", "pending")
    newer = status(2, "gate", "success")
    listed = [newer, older] if newest_first else [older, newer]
    assert forge(Wire([("gate", 222)], statuses=listed)).checks(7, SHA) == "green"


@pytest.mark.unit
def test_a_newer_status_from_another_app_does_not_supersede_the_pinned_one():
    statuses = [
        status(1, "gate", "failure"),
        status(2, "gate", "success", login="other-bot[bot]"),
    ]
    assert forge(Wire([("gate", 222)], statuses=statuses)).checks(7, SHA) == "red"


@pytest.mark.unit
def test_a_failing_pinned_run_beside_a_passing_pinned_status_reads_red():
    wire = Wire(
        [("gate", 222)],
        statuses=[status(1, "gate", "success")],
        check_runs=[run(5, "gate", conclusion="failure")],
    )
    assert forge(wire).checks(7, SHA) == "red"


@pytest.mark.unit
def test_a_pinned_run_still_meets_the_requirement_with_no_status():
    wire = Wire([("gate", 222)], check_runs=[run(5, "gate")])
    assert forge(wire).checks(7, SHA) == "green"


@pytest.mark.unit
def test_each_creator_is_looked_up_once_per_read():
    statuses = [status(i, "gate", "success") for i in range(1, 4)]
    statuses.append(status(9, "lint", "success"))
    wire = Wire([("gate", 222), ("lint", 222)], statuses=statuses)
    assert forge(wire).checks(7, SHA) == "green"
    assert wire.app_lookups == ["ci-runner"]


@pytest.mark.unit
def test_unpinned_requirements_never_read_the_status_list():
    class Unpinned(Wire):
        def pages(self, path, *, params=None, key=None):
            assert path != STATUSES
            return super().pages(path, params=params, key=key)

    wire = Unpinned([("gate", None)], check_runs=[run(5, "gate", app=111)])
    assert forge(wire).checks(7, SHA) == "green"
    assert wire.app_lookups == []


@pytest.mark.unit
def test_an_app_lookup_that_fails_otherwise_than_not_found_is_raised():
    wire = Wire(
        [("gate", 222)],
        statuses=[status(1, "gate", "success")],
        app_reply=Response(500, {}),
    )
    with pytest.raises(ForgeError):
        forge(wire).checks(7, SHA)
