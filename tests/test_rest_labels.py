"""`RestForge`'s owed-label calls and `merged`, against a fake wire."""

import pytest

from carl_merge import forge, rest
from carl_merge.transport import Response

REPO = "/repos/example/shop"
SHA = "a" * 40
OWED = "carl-merge-ci-owed"


class Wire:
    def __init__(self):
        self.calls = []
        self.replies = {}

    def on(self, method, path, reply):
        self.replies[(method, path)] = reply

    def request(
        self, method, path, *, params=None, body=None, expect=(200, 201, 202, 204)
    ):
        self.calls.append((method, path, params, body))
        reply = self.replies[(method, path)]
        if reply.status not in expect:
            raise forge.ForgeError(
                f"{method} {path}: HTTP {reply.status}", reply.status
            )
        return reply

    def pages(self, path, *, params=None, key=None):
        self.calls.append(("GET", path, params, None))
        return self.replies[("GET", path)].data

    def sent(self, method, path):
        return [c for c in self.calls if c[:2] == (method, path)]


def ok(data=None, status=200):
    return Response(status, data)


def make(wire):
    return rest.RestForge(
        wire,
        repo="example/shop",
        app_slug="carl",
        base_branch="main",
        sleep=lambda seconds: None,
    )


@pytest.mark.unit
def test_closed_with_label_asks_for_closed_items_and_drops_plain_issues():
    wire = Wire()
    wire.on(
        "GET",
        f"{REPO}/issues",
        ok(
            [
                {"number": 9, "pull_request": {"url": "x"}},
                {"number": 4},
                {"number": 2, "pull_request": {"url": "y"}},
            ]
        ),
    )
    assert make(wire).closed_with_label(OWED) == [2, 9]
    params = wire.sent("GET", f"{REPO}/issues")[0][2]
    assert params == {"labels": OWED, "state": "closed", "per_page": 100}


@pytest.mark.unit
def test_add_label_on_an_existing_label_does_not_create_it():
    wire = Wire()
    wire.on("GET", f"{REPO}/labels/{OWED}", ok({"name": OWED}))
    wire.on("POST", f"{REPO}/issues/5/labels", ok([{"name": OWED}]))
    make(wire).add_label(5, OWED)
    assert wire.sent("POST", f"{REPO}/labels") == []
    assert wire.sent("POST", f"{REPO}/issues/5/labels")[0][3] == {"labels": [OWED]}


@pytest.mark.unit
def test_add_label_creates_a_missing_label_once_then_adds_it():
    wire = Wire()
    wire.on("GET", f"{REPO}/labels/{OWED}", ok({"message": "Not Found"}, 404))
    wire.on("POST", f"{REPO}/labels", ok({"name": OWED}, 201))
    wire.on("POST", f"{REPO}/issues/5/labels", ok([{"name": OWED}]))
    make(wire).add_label(5, OWED)
    created = wire.sent("POST", f"{REPO}/labels")
    assert len(created) == 1
    assert created[0][3]["name"] == OWED
    assert set(created[0][3]) == {"name", "color", "description"}
    order = [c[1] for c in wire.calls]
    assert order == [
        f"{REPO}/labels/{OWED}",
        f"{REPO}/labels",
        f"{REPO}/issues/5/labels",
    ]


@pytest.mark.unit
def test_add_label_quotes_the_label_in_its_path():
    wire = Wire()
    wire.on("GET", f"{REPO}/labels/ci%20owed%2Fnow", ok({"name": "ci owed/now"}))
    wire.on("POST", f"{REPO}/issues/5/labels", ok([]))
    make(wire).add_label(5, "ci owed/now")
    assert wire.sent("GET", f"{REPO}/labels/ci%20owed%2Fnow")


@pytest.mark.unit
def test_add_label_raises_when_the_label_cannot_be_created():
    wire = Wire()
    wire.on("GET", f"{REPO}/labels/{OWED}", ok(None, 404))
    wire.on("POST", f"{REPO}/labels", ok({"message": "Validation Failed"}, 422))
    with pytest.raises(forge.ForgeError):
        make(wire).add_label(5, OWED)
    assert wire.sent("POST", f"{REPO}/issues/5/labels") == []


def _pull_payload(**over):
    data = {
        "draft": False,
        "mergeable": None,
        "head": {"sha": SHA, "ref": "topic", "repo": {"full_name": "example/shop"}},
        "base": {"ref": "main", "repo": {"full_name": "example/shop"}},
    }
    data.update(over)
    return data


@pytest.mark.unit
@pytest.mark.parametrize("merged", [True, False])
def test_pull_reads_merged(merged):
    wire = Wire()
    wire.on("GET", f"{REPO}/pulls/7", ok(_pull_payload(merged=merged)))
    wire.on("GET", f"{REPO}/compare/main...{SHA}", ok({"behind_by": 0}))
    assert make(wire).pull(7).merged is merged


@pytest.mark.unit
def test_pull_without_a_merged_field_is_not_merged():
    wire = Wire()
    wire.on("GET", f"{REPO}/pulls/7", ok(_pull_payload()))
    wire.on("GET", f"{REPO}/compare/main...{SHA}", ok({"behind_by": 0}))
    assert make(wire).pull(7).merged is False
