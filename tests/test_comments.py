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
