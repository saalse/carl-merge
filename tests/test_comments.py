"""Comment bodies, the marker, the dry-run prefix and reason truncation."""

import pytest

from carl_merge import comments


def _render(action: str, **extra):
    fields = {"head": "a" * 40, "base": "main", "label": "ready-to-merge"}
    return comments.render(action, **fields, **extra)


@pytest.mark.unit
def test_every_comment_ends_with_its_marker():
    body = _render("merge")
    assert body.splitlines()[0] == "Carl Mergé: merged aaaaaaaa into main."
    assert body.endswith(f"<!-- carl-merge:merge:{'a' * 40} -->")


@pytest.mark.unit
def test_dry_run_prefixes_the_line_and_the_marker():
    body = _render("bypass", dry=True)
    assert body.startswith("Dry run: ")
    assert "would merge" in body.splitlines()[0]
    assert f"carl-merge:dry-bypass:{'a' * 40}" in body


@pytest.mark.unit
def test_the_update_marker_names_the_new_head():
    body = _render("update", new_head="b" * 40)
    assert f"carl-merge:update:{'b' * 40}" in body
    assert "bbbbbbbb" in body.splitlines()[0]


@pytest.mark.unit
def test_a_dry_update_still_has_a_head_in_its_marker():
    assert f"carl-merge:dry-update:{'a' * 40}" in _render("update", dry=True)


@pytest.mark.unit
def test_markers_are_read_back_as_action_and_head():
    bodies = [_render("merge"), "no marker here", _render("cannot-decide", dry=True)]
    assert comments.markers(bodies) == {
        ("merge", "a" * 40),
        ("dry-cannot-decide", "a" * 40),
    }


@pytest.mark.unit
@pytest.mark.parametrize("dry", [False, True])
def test_the_base_red_comment_links_the_run_and_marks_head_and_run(dry: bool):
    url = "https://github.com/example/shop/actions/runs/123"
    body = _render("base-red", dry=dry, workflow="ci.yml", run_id=123, run_url=url)
    first = body.splitlines()[0]
    assert f"latest ci.yml run on main is red: {url}." in first
    assert "ready-to-merge" in first
    assert body.endswith(comments.marker("base-red", f"{'a' * 40}.123", dry=dry))
    action = "dry-base-red" if dry else "base-red"
    assert comments.markers([body]) == {(action, f"{'a' * 40}.123")}


@pytest.mark.unit
def test_the_red_after_bypass_comment_links_the_run():
    url = "https://github.com/example/shop/actions/runs/9"
    body = _render("red-after-bypass", workflow="ci.yml", run_id=9, run_url=url)
    assert body.splitlines()[0] == (
        f"Carl Mergé: the ci.yml run on main after this merge is red: {url}."
    )
    assert f"carl-merge:red-after-bypass:{'a' * 40}.9" in body


@pytest.mark.unit
def test_only_twenty_detail_lines_are_kept():
    body = _render("update", lines=[f"line {n}" for n in range(50)])
    assert "line 19" in body
    assert "line 20" not in body


@pytest.mark.unit
def test_a_long_reason_is_cut_to_one_line():
    body = _render("cannot-decide", reason="word\n" * 200)
    first = body.splitlines()[0]
    assert first.count("\n") == 0
    assert "..." in first
    assert len(first) < 400
