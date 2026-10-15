"""`RestForge`: the `Forge` protocol over the GitHub REST endpoints."""

from __future__ import annotations

import re
import time
import urllib.parse
from collections.abc import Iterator
from typing import Any, Protocol

from carl_merge.forge import (
    ForgeError,
    HeadMoved,
    MergeRefused,
    MergeTimeout,
    Pull,
    UpdateConflict,
)

RED_RUNS = ("failure", "timed_out", "startup_failure", "action_required")
IGNORED_RUNS = ("cancelled", "skipped", "neutral")
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_target")
GREEN_CHECKS = ("success", "skipped", "neutral")
OPEN_RUN_STATES = ("queued", "in_progress", "waiting", "pending", "requested")
MERGE_POLLS = 30
MAX_RUN_PAGES = 5
MERGE_WAIT = 2
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}$")
_SEVERITY = {"green": 0, "pending": 1, "red": 2}
LABEL_COLOR = "fbca04"
LABEL_DESCRIPTION = "Carl Mergé owes a full CI run on the base branch for this merge"


def _run_state(run: dict) -> str:
    if run["status"] != "completed":
        return "pending"
    return "green" if run["conclusion"] in GREEN_CHECKS else "red"


def _meets(run: dict, name: str, pinned: int | None) -> bool:
    """Does this check run carry the required name, from the pinned app if any?"""
    if run["name"] != name:
        return False
    return pinned is None or (run.get("app") or {}).get("id") == pinned


def _newest(runs: list[dict], name: str, pinned: int | None) -> dict | None:
    """The newest check run that carries the required name, or None.

    The forge counts only the newest run of each required name, so a run
    superseded in an older check suite never decides. Run ids rise with
    creation, so the highest id is the newest, and a newer run still queued
    reads pending whatever order the listing came in.
    """
    found = [run for run in runs if _meets(run, name, pinned)]
    return max(found, key=lambda run: run["id"], default=None)


def _status_state(status: dict) -> str:
    return {"success": "green", "pending": "pending"}.get(status["state"], "red")


class Wire(Protocol):
    def request(self, method: str, path: str, **kwargs: Any) -> Any: ...

    def pages(self, path: str, **kwargs: Any) -> list: ...

    def iter_pages(self, path: str, **kwargs: Any) -> Iterator[list]: ...


def _q(text: str) -> str:
    return urllib.parse.quote(text, safe="/")


def _fields(data: Any) -> dict:
    """The reply's fields, with any nested `details` object folded in."""
    if not isinstance(data, dict):
        return {}
    nested = data.get("details")
    return {**nested, **data} if isinstance(nested, dict) else data


def _uuid(data: Any) -> str:
    if isinstance(data, str):
        return data
    fields = _fields(data)
    for key in ("uuid", "id", "request_id", "merge_id"):
        if isinstance(fields.get(key), str):
            return fields[key]
    for value in fields.values():
        if isinstance(value, str) and _UUID.match(value):
            return value
    raise ForgeError("merge-async: no request id in the reply")


def _commit(data: dict) -> str:
    for key in ("merge_commit_oid", "merge_commit_sha", "sha", "oid", "commit"):
        if isinstance(data.get(key), str):
            return data[key]
    return ""


class RestForge:
    def __init__(
        self,
        wire: Wire,
        *,
        repo: str,
        app_slug: str,
        base_branch: str,
        required_checks: tuple[str, ...] = (),
        sleep=time.sleep,
    ) -> None:
        self._wire = wire
        self._repo = f"/repos/{repo}"
        self._author = f"{app_slug}[bot]"
        self._base_branch = base_branch
        self._required = required_checks
        self._sleep = sleep

    def _get(self, path: str, **params: Any) -> Any:
        return self._wire.request("GET", self._repo + path, params=params or None).data

    # -- reads -----------------------------------------------------------

    def ready_pulls(self, label: str, base: str) -> list[int]:
        found = self._wire.pages(
            f"{self._repo}/pulls",
            params={
                "state": "open",
                "base": base,
                "sort": "created",
                "direction": "asc",
                "per_page": 100,
            },
        )
        return sorted(
            p["number"] for p in found if label in [x["name"] for x in p["labels"]]
        )

    def pull(self, number: int) -> Pull:
        data = self._get(f"/pulls/{number}")
        head, base = data["head"], data["base"]
        fork = (
            head.get("repo") is None
            or head["repo"]["full_name"] != base["repo"]["full_name"]
        )
        span = f"{_q(base['ref'])}...{head['sha']}"
        behind = self._get(f"/compare/{span}")["behind_by"] > 0
        return Pull(
            number=number,
            head_sha=head["sha"],
            head_ref=head["ref"],
            draft=bool(data["draft"]),
            mergeable=data["mergeable"],
            behind=behind,
            fork=fork,
            merged=bool(data.get("merged")),
        )

    def base_head(self, branch: str) -> str:
        return self._get(f"/branches/{_q(branch)}")["commit"]["sha"]

    def base_red(self, workflow: str, branch: str) -> bool:
        """Is the latest countable completed run on the branch a failure?

        Pull request runs, and cancelled, skipped and neutral runs, do not
        count. Pages are read until a countable run turns up. A history with
        none at all is not red; one with none in the pages allowed is unknown,
        and unknown stops the pass.
        """
        pages = self._wire.iter_pages(
            f"{self._repo}/actions/workflows/{_q(workflow)}/runs",
            params={"branch": branch, "status": "completed", "per_page": 100},
            key="workflow_runs",
        )
        for number, page in enumerate(pages, 1):
            if number > MAX_RUN_PAGES:
                raise ForgeError(
                    f"no countable {workflow} run in {MAX_RUN_PAGES} pages"
                )
            for run in page:
                if run.get("event") in PULL_REQUEST_EVENTS:
                    continue
                if run["conclusion"] not in IGNORED_RUNS:
                    return run["conclusion"] in RED_RUNS
        return False

    def _requirements(self) -> list[tuple[str, int | None]]:
        """Each required check as `(name, integration id or None)`."""
        if self._required:
            return [(name, None) for name in self._required]
        rules = self._wire.pages(
            f"{self._repo}/rules/branches/{_q(self._base_branch)}",
            params={"per_page": 100},
        )
        found: list[tuple[str, int | None]] = []
        for rule in rules:
            if rule.get("type") != "required_status_checks":
                continue
            for item in rule["parameters"]["required_status_checks"]:
                need = (item["context"], item.get("integration_id"))
                if need not in found:
                    found.append(need)
        return found

    def _check_runs(self, sha: str) -> list[dict]:
        return self._wire.pages(
            f"{self._repo}/commits/{sha}/check-runs",
            params={"per_page": 100},
            key="check_runs",
        )

    def checks(self, number: int, sha: str) -> str:
        needs = self._requirements()
        if not needs:
            return "unknown"
        runs = self._check_runs(sha)
        statuses = self._get(f"/commits/{sha}/status", per_page=100)["statuses"]
        states = []
        for name, pinned in needs:
            newest = _newest(runs, name, pinned)
            found = [_run_state(newest)] if newest else []
            if pinned is None:
                found += [
                    _status_state(status)
                    for status in statuses
                    if status["context"] == name
                ]
            states.append(max(found or ["pending"], key=_SEVERITY.get))
        return max(states, key=_SEVERITY.get)

    def tested_base(self, number: int, sha: str) -> str | None:
        """The base the accepted required checks ran against, or None.

        Each required check run leads to its check suite, and each suite to the
        pull request workflow run that owns it. Only when every required check
        is such a run, and those runs all name one base, is that base the tested
        one. Otherwise there is no single tested base, and the queue updates the
        branch rather than bypass. A run from another workflow, older or newer,
        never decides it: a base the checks did not run against, the merge base
        included, can hide a change the base branch later reverted.
        """
        needs = self._requirements()
        if not needs:
            return None
        runs = self._check_runs(sha)
        suites: set[int] = set()
        for name, pinned in needs:
            newest = _newest(runs, name, pinned)
            suite = ((newest or {}).get("check_suite") or {}).get("id")
            if suite is None:
                return None
            suites.add(suite)
        workflow_runs = self._wire.pages(
            f"{self._repo}/actions/runs",
            params={"head_sha": sha, "event": "pull_request", "per_page": 100},
            key="workflow_runs",
        )
        bases: dict[int, str] = {}
        for run in workflow_runs:
            if run.get("check_suite_id") not in suites:
                continue
            for pr in run.get("pull_requests", []):
                base = (pr.get("base") or {}).get("sha")
                if pr.get("number") == number and base:
                    bases[run["check_suite_id"]] = base
        if set(bases) != suites or len(set(bases.values())) != 1:
            return None
        return next(iter(bases.values()))

    def comment_bodies(self, number: int) -> list[str]:
        found = self._wire.pages(
            f"{self._repo}/issues/{number}/comments", params={"per_page": 100}
        )
        return [
            c["body"]
            for c in found
            if (c.get("user") or {}).get("login") == self._author
        ]

    def closed_with_label(self, label: str) -> list[int]:
        """The closed pull requests, not issues, that carry the label."""
        found = self._wire.pages(
            f"{self._repo}/issues",
            params={"labels": label, "state": "closed", "per_page": 100},
        )
        return sorted(item["number"] for item in found if "pull_request" in item)

    # -- writes ----------------------------------------------------------

    def comment(self, number: int, body: str) -> None:
        self._wire.request(
            "POST", f"{self._repo}/issues/{number}/comments", body={"body": body}
        )

    def remove_label(self, number: int, label: str) -> None:
        self._wire.request(
            "DELETE",
            f"{self._repo}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}",
            expect=(200, 204, 404),
        )

    def add_label(self, number: int, label: str) -> None:
        """Add the label, creating it first when the repository lacks it."""
        quoted = urllib.parse.quote(label, safe="")
        found = self._wire.request(
            "GET", f"{self._repo}/labels/{quoted}", expect=(200, 404)
        )
        if found.status == 404:
            self._wire.request(
                "POST",
                f"{self._repo}/labels",
                body={
                    "name": label,
                    "color": LABEL_COLOR,
                    "description": LABEL_DESCRIPTION,
                },
                expect=(201,),
            )
        self._wire.request(
            "POST",
            f"{self._repo}/issues/{number}/labels",
            body={"labels": [label]},
            expect=(200,),
        )

    def _head_after(self, number: int, sha: str) -> bool:
        return self._get(f"/pulls/{number}")["head"]["sha"] != sha

    def _already(self, number: int, fields: dict) -> str:
        """A 200 means merged or queued; only a merged pull request succeeds."""
        if fields.get("status") == "enqueued":
            raise MergeRefused("the pull request is already in a merge queue")
        data = self._get(f"/pulls/{number}")
        if not data.get("merged"):
            raise MergeRefused("the pull request is not merged")
        return _commit(fields) or data.get("merge_commit_sha") or ""

    def merge(self, number: int, sha: str, method: str, bypass: bool) -> str:
        path = f"{self._repo}/pulls/{number}/merge-async"
        reply = self._wire.request(
            "PUT",
            path,
            body={
                "sha": sha,
                "merge_method": method,
                "merge_action": "direct_merge",
                "bypass_rules": bypass,
            },
            expect=(200, 202, 400, 409),
        )
        if reply.status == 400:
            raise MergeRefused("GitHub says the pull request is not ready to merge")
        if reply.status == 200:
            return self._already(number, _fields(reply.data))
        request_id = _uuid(reply.data)
        for poll in range(MERGE_POLLS):
            if poll:
                self._sleep(MERGE_WAIT)
            result = _fields(self._wire.request("GET", f"{path}/{request_id}").data)
            status = result.get("status")
            if status == "merged":
                return _commit(result)
            if status == "enqueued":
                raise MergeRefused("the pull request went into a merge queue")
            if status == "failed":
                if self._head_after(number, sha):
                    raise HeadMoved("the head moved while the merge ran")
                raise MergeRefused(str(result.get("message") or "the merge failed"))
        raise MergeTimeout("the merge had not finished after the polls")

    def update_branch(self, number: int, expected_head_sha: str) -> None:
        reply = self._wire.request(
            "PUT",
            f"{self._repo}/pulls/{number}/update-branch",
            body={"expected_head_sha": expected_head_sha},
            expect=(200, 202, 422),
        )
        if reply.status == 422:
            if self._head_after(number, expected_head_sha):
                raise HeadMoved("the head moved before the update")
            raise UpdateConflict("the update hit a merge conflict")

    def cancel_runs(self, number: int, head_ref: str) -> int:
        cancelled = 0
        for state in OPEN_RUN_STATES:
            runs = self._get(
                "/actions/runs",
                branch=head_ref,
                event="pull_request",
                status=state,
                per_page=100,
            )["workflow_runs"]
            for run in runs:
                if number not in [pr.get("number") for pr in run["pull_requests"]]:
                    continue
                done = self._wire.request(
                    "POST",
                    f"{self._repo}/actions/runs/{run['id']}/cancel",
                    expect=(202, 409),
                )
                cancelled += done.status == 202
        return cancelled

    def dispatch(self, workflow: str, branch: str) -> None:
        self._wire.request(
            "POST",
            f"{self._repo}/actions/workflows/{_q(workflow)}/dispatches",
            body={"ref": branch},
            expect=(200, 204),
        )
