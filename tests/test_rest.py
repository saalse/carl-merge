"""Each `RestForge` method against a fake wire that records its requests."""

import itertools

import pytest

from carl_merge import cli, forge, rest
from carl_merge.transport import Response

REPO = "/repos/example/shop"
SHA = "a" * 40


class Wire:
    def __init__(self):
        self.calls = []
        self.replies = {}

    def on(self, method, path, *replies):
        self.replies[(method, path)] = list(replies)

    def _next(self, method, path):
        queue = self.replies[(method, path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def request(
        self, method, path, *, params=None, body=None, expect=(200, 201, 202, 204)
    ):
        self.calls.append((method, path, params, body))
        reply = self._next(method, path)
        if reply.status not in expect:
            raise forge.ForgeError(
                f"{method} {path}: HTTP {reply.status}", reply.status
            )
        return reply

    def pages_on(self, path, *pages):
        self.replies[("GET", path)] = [list(page) for page in pages]
        self.paged = getattr(self, "paged", set()) | {path}

    def iter_pages(self, path, *, params=None, key=None):
        self.calls.append(("GET", path, params, None))
        for page in self.replies[("GET", path)]:
            self.fetched = getattr(self, "fetched", 0) + 1
            yield page

    def pages(self, path, *, params=None, key=None):
        if path in getattr(self, "paged", set()):
            pages = self.iter_pages(path, params=params, key=key)
            return [item for page in pages for item in page]
        self.calls.append(("GET", path, params, None))
        return self._next("GET", path)

    def sent(self, method, path):
        return [c for c in self.calls if c[:2] == (method, path)]


def ok(data=None, status=200):
    return Response(status, data)


def make(wire, **kwargs):
    return rest.RestForge(
        wire,
        repo="example/shop",
        app_slug="carl",
        base_branch="main",
        sleep=lambda seconds: None,
        **kwargs,
    )


def pull_payload(**over):
    data = {
        "draft": False,
        "mergeable": True,
        "head": {"sha": SHA, "ref": "topic", "repo": {"full_name": "example/shop"}},
        "base": {"ref": "main", "repo": {"full_name": "example/shop"}},
    }
    data.update(over)
    return data


@pytest.mark.unit
def test_ready_pulls_keeps_the_label_and_sorts():
    wire = Wire()
    labelled = [{"name": "ready-to-merge"}]
    wire.on(
        "GET",
        f"{REPO}/pulls",
        [
            {"number": 9, "labels": labelled, "head": {"sha": "h9"}},
            {"number": 3, "labels": [{"name": "docs"}], "head": {"sha": "h3"}},
            {"number": 4, "labels": labelled, "head": {"sha": "h4"}},
        ],
    )
    rest = make(wire)
    assert rest.ready_pulls("ready-to-merge", "main") == [4, 9]
    assert (rest.listed_head(4), rest.listed_head(9)) == ("h4", "h9")
    params = wire.sent("GET", f"{REPO}/pulls")[0][2]
    assert params["base"] == "main"
    assert params["direction"] == "asc"


@pytest.mark.unit
def test_listed_head_reads_a_pull_request_the_listing_did_not_hold():
    wire = Wire()
    wire.on("GET", f"{REPO}/pulls/5", ok({"head": {"sha": "h5"}}))
    assert make(wire).listed_head(5) == "h5"


@pytest.mark.unit
@pytest.mark.parametrize(
    "head_repo, behind_by, fork, behind",
    [
        ({"full_name": "example/shop"}, 0, False, False),
        ({"full_name": "example/shop"}, 2, False, True),
        ({"full_name": "someone/shop"}, 0, True, False),
        (None, 0, True, False),
    ],
)
def test_pull_reads_fork_and_behind(head_repo, behind_by, fork, behind):
    wire = Wire()
    data = pull_payload()
    data["head"]["repo"] = head_repo
    wire.on("GET", f"{REPO}/pulls/7", ok(data))
    wire.on("GET", f"{REPO}/compare/main...{SHA}", ok({"behind_by": behind_by}))
    pull = make(wire).pull(7)
    assert (pull.fork, pull.behind) == (fork, behind)
    assert (pull.number, pull.head_sha, pull.head_ref) == (7, SHA, "topic")
    assert pull.mergeable is True


@pytest.mark.unit
def test_an_unknown_mergeable_is_passed_through():
    wire = Wire()
    wire.on("GET", f"{REPO}/pulls/7", ok(pull_payload(mergeable=None)))
    wire.on("GET", f"{REPO}/compare/main...{SHA}", ok({"behind_by": 0}))
    assert make(wire).pull(7).mergeable is None


@pytest.mark.unit
def test_base_head_reads_the_branch():
    wire = Wire()
    wire.on("GET", f"{REPO}/branches/main", ok({"commit": {"sha": "b" * 40}}))
    assert make(wire).base_head("main") == "b" * 40


def wf_run(conclusion, sha, *, event="push", id=1, status="completed"):
    return {
        "id": id,
        "html_url": f"https://github.com/example/shop/actions/runs/{id}",
        "head_sha": sha,
        "event": event,
        "status": status,
        "conclusion": conclusion,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "conclusions, red",
    [
        ([], False),
        (["success"], False),
        (["failure"], True),
        (["cancelled", "skipped", "neutral", "failure"], True),
        (["cancelled", "success", "failure"], False),
        (["timed_out"], True),
        (["cancelled"], False),
    ],
)
def test_base_red_reads_the_latest_countable_run(conclusions, red):
    wire = Wire()
    runs = [wf_run(c, f"c{n}", id=n) for n, c in enumerate(conclusions)]
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", runs)
    assert (make(wire).base_red("ci.yml", "main") is not None) is red
    params = wire.sent("GET", f"{REPO}/actions/workflows/ci.yml/runs")[0][2]
    assert params["status"] == "completed"
    assert params["branch"] == "main"


def checks_wire(runs, statuses=(), rules=None):
    wire = Wire()
    wire.on("GET", f"{REPO}/commits/{SHA}/check-runs", list(runs))
    wire.on("GET", f"{REPO}/commits/{SHA}/status", ok({"statuses": list(statuses)}))
    if rules is not None:
        wire.on("GET", f"{REPO}/rules/branches/main", rules)
    return wire


run_ids = itertools.count(1)


def run(name, status="completed", conclusion="success"):
    return {
        "id": next(run_ids),
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


@pytest.mark.unit
def test_checks_are_green_when_every_required_name_passed():
    wire = checks_wire([run("lint"), run("tests", conclusion="skipped")])
    assert make(wire, required_checks=("lint", "tests")).checks(1, SHA) == "green"


@pytest.mark.unit
def test_a_failed_check_run_is_red():
    wire = checks_wire([run("lint"), run("tests", conclusion="failure")])
    assert make(wire, required_checks=("lint", "tests")).checks(1, SHA) == "red"


@pytest.mark.unit
def test_a_missing_name_is_pending():
    wire = checks_wire([run("lint")])
    assert make(wire, required_checks=("lint", "tests")).checks(1, SHA) == "pending"


@pytest.mark.unit
def test_an_unfinished_run_is_pending_and_red_wins_over_pending():
    wire = checks_wire([run("lint", status="in_progress", conclusion=None)])
    assert make(wire, required_checks=("lint",)).checks(1, SHA) == "pending"
    wire = checks_wire(
        [run("lint", status="queued", conclusion=None), run("t", conclusion="failure")]
    )
    assert make(wire, required_checks=("lint", "t")).checks(1, SHA) == "red"


@pytest.mark.unit
@pytest.mark.parametrize(
    "state, expected",
    [("success", "green"), ("failure", "red"), ("pending", "pending")],
)
def test_a_status_context_counts_as_a_check(state, expected):
    wire = checks_wire([], statuses=[{"context": "legacy/ci", "state": state}])
    assert make(wire, required_checks=("legacy/ci",)).checks(1, SHA) == expected


@pytest.mark.unit
def test_required_names_come_from_the_branch_rules_when_not_configured():
    rules = [
        {"type": "pull_request"},
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "lint"}]},
        },
    ]
    wire = checks_wire([run("lint")], rules=rules)
    assert make(wire).checks(1, SHA) == "green"


@pytest.mark.unit
def test_no_required_names_anywhere_is_unknown():
    wire = checks_wire([], rules=[{"type": "pull_request"}])
    assert make(wire).checks(1, SHA) == "unknown"


@pytest.mark.unit
def test_comment_bodies_drops_other_authors():
    wire = Wire()
    wire.on(
        "GET",
        f"{REPO}/issues/5/comments",
        [
            {"user": {"login": "carl[bot]"}, "body": "mine"},
            {"user": {"login": "someone"}, "body": "<!-- carl-merge:merge:x -->"},
            {"user": None, "body": "ghost"},
        ],
    )
    assert make(wire).comment_bodies(5) == ["mine"]


@pytest.mark.unit
def test_comment_and_remove_label():
    wire = Wire()
    wire.on("POST", f"{REPO}/issues/5/comments", ok({}, 201))
    wire.on("DELETE", f"{REPO}/issues/5/labels/ready%20to%20merge", ok(None, 404))
    forge_ = make(wire)
    forge_.comment(5, "hello")
    forge_.remove_label(5, "ready to merge")
    assert wire.sent("POST", f"{REPO}/issues/5/comments")[0][3] == {"body": "hello"}
    assert len(wire.sent("DELETE", f"{REPO}/issues/5/labels/ready%20to%20merge")) == 1


MERGE = f"{REPO}/pulls/5/merge-async"
UUID = "123e4567-e89b-12d3-a456-426614174000"


@pytest.mark.unit
@pytest.mark.parametrize("bypass", [True, False])
def test_merge_sends_bypass_exactly_as_passed(bypass):
    wire = Wire()
    wire.on("PUT", MERGE, ok({"id": UUID}, 202))
    wire.on(
        "GET", f"{MERGE}/{UUID}", ok({"status": "merged", "merge_commit_oid": "m" * 40})
    )
    assert make(wire).merge(5, SHA, "squash", bypass) == "m" * 40
    assert wire.sent("PUT", MERGE)[0][3] == {
        "sha": SHA,
        "merge_method": "squash",
        "merge_action": "direct_merge",
        "bypass_rules": bypass,
    }


@pytest.mark.unit
def test_merge_polls_a_pending_request_until_it_is_merged():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"uuid": UUID}, 202))
    wire.on(
        "GET",
        f"{MERGE}/{UUID}",
        ok({"status": "pending"}),
        ok({"status": "pending"}),
        ok({"status": "merged", "merge_commit_oid": "m" * 40}),
    )
    assert make(wire).merge(5, SHA, "merge", False) == "m" * 40
    assert len(wire.sent("GET", f"{MERGE}/{UUID}")) == 3


@pytest.mark.unit
def test_a_409_follows_the_request_already_pending():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"id": UUID}, 409))
    wire.on("GET", f"{MERGE}/{UUID}", ok({"status": "merged", "sha": "m" * 40}))
    assert make(wire).merge(5, SHA, "merge", False) == "m" * 40


@pytest.mark.unit
def test_a_failed_merge_with_the_same_head_is_refused():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"id": UUID}, 202))
    wire.on(
        "GET",
        f"{MERGE}/{UUID}",
        ok({"status": "failed", "message": "blocked by rules"}),
    )
    wire.on("GET", f"{REPO}/pulls/5", ok(pull_payload()))
    with pytest.raises(forge.MergeRefused, match="blocked by rules"):
        make(wire).merge(5, SHA, "merge", True)


@pytest.mark.unit
def test_a_failed_merge_with_a_moved_head_is_head_moved():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"id": UUID}, 202))
    wire.on("GET", f"{MERGE}/{UUID}", ok({"status": "failed", "message": "x"}))
    moved = pull_payload()
    moved["head"]["sha"] = "f" * 40
    wire.on("GET", f"{REPO}/pulls/5", ok(moved))
    with pytest.raises(forge.HeadMoved):
        make(wire).merge(5, SHA, "merge", False)


@pytest.mark.unit
def test_a_merge_still_pending_after_the_polls_times_out():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"id": UUID}, 202))
    wire.on("GET", f"{MERGE}/{UUID}", ok({"status": "pending"}))
    with pytest.raises(forge.MergeTimeout):
        make(wire).merge(5, SHA, "merge", False)
    assert len(wire.sent("GET", f"{MERGE}/{UUID}")) == rest.MERGE_POLLS


@pytest.mark.unit
def test_400_is_refused():
    wire = Wire()
    wire.on("PUT", MERGE, ok(None, 400))
    with pytest.raises(forge.MergeRefused):
        make(wire).merge(5, SHA, "merge", False)


@pytest.mark.unit
def test_a_200_succeeds_only_for_a_merged_pull_request():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"status": "merged", "merge_commit_oid": "m" * 40}, 200))
    wire.on("GET", f"{REPO}/pulls/5", ok({"merged": True}))
    assert make(wire).merge(5, SHA, "merge", False) == "m" * 40
    wire.on("PUT", MERGE, ok({}, 200))
    wire.on(
        "GET", f"{REPO}/pulls/5", ok({"merged": True, "merge_commit_sha": "c" * 40})
    )
    assert make(wire).merge(5, SHA, "merge", False) == "c" * 40


@pytest.mark.unit
def test_a_200_for_a_queued_or_unmerged_pull_request_is_refused():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"status": "enqueued"}, 200))
    with pytest.raises(forge.MergeRefused, match="merge queue"):
        make(wire).merge(5, SHA, "merge", False)
    assert wire.sent("GET", f"{REPO}/pulls/5") == []
    wire.on("PUT", MERGE, ok({}, 200))
    wire.on("GET", f"{REPO}/pulls/5", ok({"merged": False}))
    with pytest.raises(forge.MergeRefused, match="not merged"):
        make(wire).merge(5, SHA, "merge", False)


@pytest.mark.unit
def test_a_polled_enqueued_result_is_refused_not_waited_on():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"id": UUID}, 202))
    wire.on("GET", f"{MERGE}/{UUID}", ok({"status": "enqueued"}))
    with pytest.raises(forge.MergeRefused, match="merge queue"):
        make(wire).merge(5, SHA, "merge", False)
    assert len(wire.sent("GET", f"{MERGE}/{UUID}")) == 1


UPDATE = f"{REPO}/pulls/5/update-branch"


@pytest.mark.unit
def test_update_branch_pins_the_expected_head():
    wire = Wire()
    wire.on("PUT", UPDATE, ok({}, 202))
    make(wire).update_branch(5, SHA)
    assert wire.sent("PUT", UPDATE)[0][3] == {"expected_head_sha": SHA}


@pytest.mark.unit
def test_a_422_is_head_moved_or_a_conflict_by_the_head_it_finds():
    wire = Wire()
    wire.on("PUT", UPDATE, ok(None, 422))
    wire.on("GET", f"{REPO}/pulls/5", ok(pull_payload()))
    with pytest.raises(forge.UpdateConflict):
        make(wire).update_branch(5, SHA)
    with pytest.raises(forge.HeadMoved):
        make(wire).update_branch(5, "e" * 40)


def mine(run_id, number=5):
    return {"id": run_id, "pull_requests": [{"number": number}]}


@pytest.mark.unit
def test_cancel_runs_counts_only_the_runs_it_cancelled():
    wire = Wire()
    wire.replies[("GET", f"{REPO}/actions/runs")] = [
        ok({"workflow_runs": [mine(11)]}),
        ok({"workflow_runs": []}),
        ok({"workflow_runs": [mine(12)]}),
        ok({"workflow_runs": []}),
        ok({"workflow_runs": []}),
    ]
    wire.on("POST", f"{REPO}/actions/runs/11/cancel", ok({}, 202))
    wire.on("POST", f"{REPO}/actions/runs/12/cancel", ok({}, 409))
    assert make(wire).cancel_runs(5, "topic") == 1


@pytest.mark.unit
def test_cancel_runs_leaves_another_pull_requests_runs_alone():
    wire = Wire()
    others = [mine(21, number=6), {"id": 22, "pull_requests": []}, mine(23)]
    wire.replies[("GET", f"{REPO}/actions/runs")] = [
        ok({"workflow_runs": others}),
        *[ok({"workflow_runs": []})] * 4,
    ]
    wire.on("POST", f"{REPO}/actions/runs/23/cancel", ok({}, 202))
    assert make(wire).cancel_runs(5, "topic") == 1
    cancelled = [c[1] for c in wire.calls if c[0] == "POST"]
    assert cancelled == [f"{REPO}/actions/runs/23/cancel"]


@pytest.mark.unit
def test_dispatch_posts_the_ref():
    wire = Wire()
    wire.on("POST", f"{REPO}/actions/workflows/ci.yml/dispatches", ok(None, 204))
    make(wire).dispatch("ci.yml", "main")
    sent = wire.sent("POST", f"{REPO}/actions/workflows/ci.yml/dispatches")
    assert sent[0][3] == {"ref": "main"}


@pytest.mark.unit
def test_an_unreadable_base_raises_and_the_cli_exits_3(monkeypatch, tmp_path, capsys):
    wire = Wire()
    wire.on("GET", f"{REPO}/branches/main", ok(None, 500))
    with pytest.raises(forge.ForgeError):
        make(wire).base_head("main")

    def fail(*args, **kwargs):
        raise forge.ForgeError("GET /x: HTTP 500", 500)

    config = tmp_path / "carl-merge.toml"
    config.write_text(
        'ready_label = "r"\nbase_branch = "main"\nci_workflow = "ci.yml"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "run_pass", fail)
    monkeypatch.setenv("CARL_MERGE_TOKEN", "t")
    monkeypatch.setenv("CARL_MERGE_APP_SLUG", "carl")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/shop")
    assert cli.main(["run", "--config", str(config)]) == 3
    assert "HTTP 500" in capsys.readouterr().err


@pytest.mark.unit
def test_merge_reads_the_request_id_and_result_from_a_details_object():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"details": {"uuid": UUID}}, 202))
    wire.on(
        "GET",
        f"{MERGE}/{UUID}",
        ok({"details": {"status": "merged", "merge_commit_oid": "m" * 40}}),
    )
    assert make(wire).merge(5, SHA, "merge", True) == "m" * 40
    assert len(wire.sent("GET", f"{MERGE}/{UUID}")) == 1


@pytest.mark.unit
def test_a_409_with_the_id_under_details_is_followed():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"details": {"uuid": UUID}}, 409))
    wire.on("GET", f"{MERGE}/{UUID}", ok({"status": "merged", "sha": "m" * 40}))
    assert make(wire).merge(5, SHA, "merge", False) == "m" * 40


@pytest.mark.unit
def test_a_reply_with_no_request_id_raises():
    wire = Wire()
    wire.on("PUT", MERGE, ok({"details": {}}, 202))
    with pytest.raises(forge.ForgeError, match="no request id"):
        make(wire).merge(5, SHA, "merge", False)


def pr(number, base):
    return {"number": number, "base": {"sha": base}}


def pinned_rules(integration_id):
    return [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "lint", "integration_id": integration_id}
                ]
            },
        }
    ]


def app_run(name, app_id, conclusion="success"):
    return {**run(name, conclusion=conclusion), "app": {"id": app_id}}


@pytest.mark.unit
def test_a_pinned_check_is_met_only_by_its_own_integration():
    wire = checks_wire([app_run("lint", 111)], rules=pinned_rules(222))
    assert make(wire).checks(1, SHA) == "pending"
    wire = checks_wire([app_run("lint", 222)], rules=pinned_rules(222))
    assert make(wire).checks(1, SHA) == "green"


@pytest.mark.unit
def test_a_failed_check_from_another_app_does_not_fail_a_pinned_one():
    runs = [app_run("lint", 111, conclusion="failure"), app_run("lint", 222)]
    wire = checks_wire(runs, rules=pinned_rules(222))
    assert make(wire).checks(1, SHA) == "green"


@pytest.mark.unit
def test_a_commit_status_never_meets_a_pinned_check():
    wire = checks_wire(
        [], statuses=[{"context": "lint", "state": "success"}], rules=pinned_rules(222)
    )
    assert make(wire).checks(1, SHA) == "pending"


@pytest.mark.unit
def test_an_unpinned_rule_accepts_any_app():
    wire = checks_wire([app_run("lint", 111)], rules=pinned_rules(None))
    assert make(wire).checks(1, SHA) == "green"


@pytest.mark.unit
def test_every_page_of_the_branch_rules_is_read():
    wire = Wire()
    rules = f"{REPO}/rules/branches/main"
    wire.pages_on(rules, [{"type": "pull_request"}], pinned_rules(222))
    wire.on("GET", f"{REPO}/commits/{SHA}/check-runs", [app_run("lint", 111)])
    wire.on("GET", f"{REPO}/commits/{SHA}/status", ok({"statuses": []}))
    assert make(wire).checks(1, SHA) == "pending"
    assert wire.fetched == 2
    assert wire.sent("GET", rules)[0][2]["per_page"] == 100


@pytest.mark.unit
def test_base_red_ignores_pull_request_runs_on_a_same_named_branch():
    wire = Wire()
    runs = [
        wf_run("success", "c1", event="pull_request", id=2),
        wf_run("failure", "c0", id=1),
    ]
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", runs)
    assert make(wire).base_red("ci.yml", "main").id == 1
    runs = [wf_run("failure", "c0", event="pull_request")]
    wire = Wire()
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", runs)
    assert make(wire).base_red("ci.yml", "main") is None


@pytest.mark.unit
def test_base_red_pages_past_runs_that_do_not_count():
    wire = Wire()
    ignored = [
        wf_run("cancelled", "c2", id=4),
        wf_run("x", "c2", event="pull_request", id=3),
    ]
    wire.pages_on(
        f"{REPO}/actions/workflows/ci.yml/runs",
        ignored,
        [wf_run("failure", "c1", id=2)],
        [wf_run("success", "c0", id=1)],
    )
    assert make(wire).base_red("ci.yml", "main").id == 2
    assert wire.fetched == 2


@pytest.mark.unit
def test_base_red_is_unknown_when_the_pages_allowed_hold_no_countable_run():
    wire = Wire()
    page = [wf_run("cancelled", "c0")]
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", *[page] * 6)
    with pytest.raises(forge.ForgeError, match="no countable"):
        make(wire).base_red("ci.yml", "main")
    wire = Wire()
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", *[page] * 5)
    assert make(wire).base_red("ci.yml", "main") is None


@pytest.mark.unit
@pytest.mark.parametrize("dispatch_first", [True, False])
def test_base_red_lets_a_red_run_outrank_a_green_one_on_the_same_commit(
    dispatch_first,
):
    """The two-second race: a lint-only push run and the full run, one commit."""
    push = wf_run("success", "c1", event="push", id=10)
    full = wf_run("failure", "c1", event="workflow_dispatch", id=11)
    older = wf_run("success", "c0", event="push", id=9)
    runs = [full, push, older] if dispatch_first else [push, full, older]
    wire = Wire()
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", runs)
    red = make(wire).base_red("ci.yml", "main")
    assert red == forge.Run(
        11,
        "https://github.com/example/shop/actions/runs/11",
        "c1",
        "workflow_dispatch",
        since="c0",
    )


@pytest.mark.unit
def test_base_red_reads_only_the_newest_counted_commit():
    wire = Wire()
    runs = [
        wf_run("success", "c1", id=3),
        wf_run("failure", "c0", id=2),
        wf_run("failure", "c1", id=1),
    ]
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", runs)
    assert make(wire).base_red("ci.yml", "main") is None


@pytest.mark.unit
def test_base_red_reads_the_deciding_commit_across_a_page_break():
    wire = Wire()
    wire.pages_on(
        f"{REPO}/actions/workflows/ci.yml/runs",
        [wf_run("success", "c1", id=3)],
        [wf_run("failure", "c1", event="workflow_dispatch", id=2)],
        [wf_run("success", "c0", id=1)],
    )
    red = make(wire).base_red("ci.yml", "main")
    assert (red.id, red.since) == (2, "c0")
    assert wire.fetched == 3


@pytest.mark.unit
def test_base_red_counts_only_the_configured_events():
    """A lint-only push run neither reds nor greens a base whose full runs decide."""
    runs = [
        wf_run("success", "c2", event="push", id=4),
        wf_run("cancelled", "c1", event="workflow_dispatch", id=3),
        wf_run("failure", "c1", event="schedule", id=2),
        wf_run("success", "c0", event="workflow_dispatch", id=1),
    ]
    wire = Wire()
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", runs)
    events = ("workflow_dispatch", "schedule")
    assert make(wire, base_events=events).base_red("ci.yml", "main").id == 2
    wire = Wire()
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", runs)
    assert make(wire).base_red("ci.yml", "main") is None


@pytest.mark.unit
def test_base_red_with_events_none_of_which_ran_is_unknown_past_the_cap():
    wire = Wire()
    page = [wf_run("failure", "c0", event="push")]
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", *[page] * 6)
    with pytest.raises(forge.ForgeError, match="no countable"):
        make(wire, base_events=("schedule",)).base_red("ci.yml", "main")


@pytest.mark.unit
def test_base_red_reads_green_when_one_commit_fills_the_pages_with_green():
    """A quiet base with a frequent schedule: 500 green runs are a verdict."""
    wire = Wire()
    page = [wf_run("success", "c1")]
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", *[page] * 6)
    assert make(wire).base_red("ci.yml", "main") is None


@pytest.mark.unit
def test_base_red_keeps_a_red_dispatched_run_found_before_the_cap():
    wire = Wire()
    first = [wf_run("failure", "c1", event="workflow_dispatch", id=9)]
    rest = [wf_run("success", "c1")]
    wire.pages_on(f"{REPO}/actions/workflows/ci.yml/runs", first, *[rest] * 5)
    red = make(wire).base_red("ci.yml", "main")
    assert (red.id, red.since) == (9, None)


@pytest.mark.unit
def test_a_red_push_run_is_returned_without_reading_further():
    wire = Wire()
    wire.pages_on(
        f"{REPO}/actions/workflows/ci.yml/runs",
        [wf_run("failure", "c1", id=2)],
        [wf_run("success", "c0", id=1)],
    )
    red = make(wire).base_red("ci.yml", "main")
    assert (red.id, red.since, wire.fetched) == (2, None, 1)


@pytest.mark.unit
def test_base_run_open_counts_only_open_runs_of_counted_events():
    path = f"{REPO}/actions/workflows/ci.yml/runs"
    cases = [
        ([wf_run(None, "c1", status="in_progress")], (), True),
        ([wf_run(None, "c1", status="queued", event="workflow_dispatch")], (), True),
        ([wf_run("success", "c1")], (), False),
        ([wf_run(None, "c1", event="pull_request", status="queued")], (), False),
        ([wf_run(None, "c1", status="in_progress")], ("workflow_dispatch",), False),
    ]
    for runs, events, open_ in cases:
        wire = Wire()
        wire.on("GET", path, runs)
        assert make(wire, base_events=events).base_run_open("ci.yml", "main", "c1") is (
            open_
        )
        params = wire.sent("GET", path)[0][2]
        assert (params["head_sha"], params["branch"]) == ("c1", "main")


@pytest.mark.unit
def test_merged_pulls_keeps_the_pull_requests_merged_as_that_commit():
    wire = Wire()
    wire.on(
        "GET",
        f"{REPO}/commits/{SHA}/pulls",
        ok(
            [
                {"number": 5, "merge_commit_sha": SHA, "merged_at": "2026-10-14"},
                {"number": 6, "merge_commit_sha": "c" * 40, "merged_at": "x"},
                {"number": 7, "merge_commit_sha": SHA, "merged_at": None},
            ]
        ),
    )
    assert make(wire).merged_pulls(SHA) == [5]


def _pulls(number, sha):
    return ok([{"number": number, "merge_commit_sha": sha, "merged_at": "x"}])


def _commit(sha, *parents):
    return {"sha": sha, "parents": [{"sha": p} for p in parents]}


@pytest.mark.unit
def test_merged_pulls_since_walks_the_base_and_skips_a_merged_branchs_commits():
    """Squash bypasses #7 and #8, then a hand merge #9 of a two-commit branch."""
    wire = Wire()
    history = [
        _commit("m9", "s8", "b2"),
        _commit("b2", "b1"),
        _commit("b1", "g0"),
        _commit("s8", "s7"),
        _commit("s7", "g0"),
        _commit("g0", "older"),
    ]
    wire.on("GET", f"{REPO}/commits", ok(history))
    for number, sha in ((9, "m9"), (8, "s8"), (7, "s7")):
        wire.on("GET", f"{REPO}/commits/{sha}/pulls", _pulls(number, sha))
    assert make(wire).merged_pulls("m9", "g0") == [7, 8, 9]
    assert wire.sent("GET", f"{REPO}/commits/b1/pulls") == []
    assert wire.sent("GET", f"{REPO}/commits/g0/pulls") == []
    assert wire.sent("GET", f"{REPO}/commits")[0][2]["sha"] == "m9"


@pytest.mark.unit
def test_merged_pulls_since_reads_a_base_commit_the_listing_crowded_out():
    """A long merged branch fills the listing; the walk reads the rest itself."""
    wire = Wire()
    branch = [_commit(f"b{i}", f"b{i - 1}") for i in range(99, 0, -1)]
    wire.on("GET", f"{REPO}/commits", ok([_commit("m20", "s19", "b99"), *branch]))
    wire.on("GET", f"{REPO}/commits/s19", ok(_commit("s19", "s18")))
    wire.on("GET", f"{REPO}/commits/s18", ok(_commit("s18", "g0")))
    for number, sha in ((20, "m20"), (19, "s19"), (18, "s18")):
        wire.on("GET", f"{REPO}/commits/{sha}/pulls", _pulls(number, sha))
    assert make(wire).merged_pulls("m20", "g0") == [18, 19, 20]
    assert wire.sent("GET", f"{REPO}/commits/m20") == []


@pytest.mark.unit
def test_merged_pulls_since_reads_the_newest_commits_of_a_long_range():
    """The head the run tested is read first, however far back `since` is."""
    wire = Wire()
    history = [_commit(f"c{i}", f"c{i - 1}") for i in range(400, 300, -1)]
    wire.on("GET", f"{REPO}/commits", ok(history))
    for i in range(400, 370, -1):
        wire.on("GET", f"{REPO}/commits/c{i}/pulls", _pulls(i, f"c{i}"))
    found = make(wire).merged_pulls("c400", "c0")
    assert found == list(range(371, 401))
    assert wire.sent("GET", f"{REPO}/commits/c370/pulls") == []


RUNS = f"{REPO}/actions/runs"
CHECKS = f"{REPO}/commits/{SHA}/check-runs"


def suite_run(name, suite, app_id=1):
    return {**app_run(name, app_id), "check_suite": {"id": suite}}


def workflow_run(suite, base, number=7):
    return {
        "check_suite_id": suite,
        "pull_requests": [{"number": number, "base": {"sha": base}}],
    }


def read_tested(check_runs, *run_pages, required=("lint", "tests")):
    wire = Wire()
    wire.pages_on(CHECKS, check_runs)
    wire.pages_on(RUNS, *run_pages)
    return make(wire, required_checks=required).tested_base(7, SHA), wire


@pytest.mark.unit
def test_tested_base_is_the_base_of_the_runs_behind_the_accepted_checks():
    checks = [suite_run("lint", 1), suite_run("tests", 2)]
    runs = [
        workflow_run(9, "n" * 40),
        workflow_run(1, "t" * 40),
        workflow_run(8, "o" * 40),
        workflow_run(2, "t" * 40),
    ]
    assert read_tested(checks, runs)[0] == "t" * 40


@pytest.mark.unit
def test_tested_base_reads_the_owning_run_from_a_later_page():
    checks = [suite_run("lint", 1)]
    base, wire = read_tested(
        checks,
        [workflow_run(9, "n" * 40)],
        [workflow_run(1, "t" * 40)],
        required=("lint",),
    )
    assert base == "t" * 40
    assert wire.fetched == 3


@pytest.mark.unit
def test_required_checks_run_against_different_bases_have_no_tested_base():
    checks = [suite_run("lint", 1), suite_run("tests", 2)]
    runs = [workflow_run(1, "a" * 40), workflow_run(2, "b" * 40)]
    assert read_tested(checks, runs)[0] is None


@pytest.mark.unit
def test_a_required_check_that_is_not_an_actions_run_gives_no_tested_base():
    checks = [suite_run("lint", 1)]
    assert read_tested(checks, [workflow_run(1, "t" * 40)])[0] is None
    checks = [suite_run("lint", 1), suite_run("tests", 5)]
    assert read_tested(checks, [workflow_run(1, "t" * 40)])[0] is None


@pytest.mark.unit
def test_another_pull_requests_run_on_the_same_suite_does_not_count():
    checks = [suite_run("lint", 1)]
    runs = [workflow_run(1, "x" * 40, number=8)]
    assert read_tested(checks, runs, required=("lint",))[0] is None
