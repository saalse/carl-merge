"""One TOML file in the adopting repository configures the bot."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from carl_merge.presets import PRESETS

REQUIRED = ("ready_label", "base_branch", "ci_workflow")
OPTIONAL = (
    "merge_method",
    "dry_run",
    "on_red_base",
    "base_events",
    "required_checks",
    "shared_risk",
    "added_together",
    "presets",
)
MERGE_METHODS = ("merge", "squash")
ON_RED_BASE = ("stop", "merge-tested")
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_target")
# The events that start GitHub Actions workflow runs today. A name outside the
# list gets a warning, not an error: it is most likely misspelt and would count
# nothing, but GitHub may have added it since this release.
WORKFLOW_EVENTS = (
    "branch_protection_rule",
    "check_run",
    "check_suite",
    "create",
    "delete",
    "deployment",
    "deployment_status",
    "discussion",
    "discussion_comment",
    "dynamic",
    "fork",
    "gollum",
    "issue_comment",
    "issues",
    "label",
    "merge_group",
    "milestone",
    "page_build",
    "public",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_target",
    "push",
    "registry_package",
    "release",
    "repository_dispatch",
    "schedule",
    "status",
    "watch",
    "workflow_call",
    "workflow_dispatch",
    "workflow_run",
)


class ConfigError(ValueError):
    """The configuration is missing a key, or holds a wrong or unknown one."""


@dataclass(frozen=True)
class Rule:
    pattern: str
    reason: str


@dataclass(frozen=True)
class Config:
    ready_label: str
    base_branch: str
    ci_workflow: str
    merge_method: str = "merge"
    dry_run: bool = True
    on_red_base: str = "stop"
    base_events: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    shared_risk: tuple[Rule, ...] = ()
    added_together: tuple[Rule, ...] = ()
    presets: tuple[str, ...] = ()
    # What parsing noticed but did not refuse, for the command line to print.
    warnings: tuple[str, ...] = ()


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string")
    return value


def _rules(data: dict, key: str) -> tuple[Rule, ...]:
    entries = data.get(key, [])
    if not isinstance(entries, list):
        raise ConfigError(f"{key} must be an array of tables")
    rules = []
    for index, entry in enumerate(entries):
        where = f"{key}[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a table")
        unknown = sorted(set(entry) - {"pattern", "reason"})
        if unknown:
            raise ConfigError(f"{where}: unknown key {unknown[0]}")
        pattern = _text(entry.get("pattern"), f"{where}.pattern")
        if pattern.startswith(("/", "./")):
            raise ConfigError(
                f"{where}.pattern: paths are relative to the repository root"
            )
        rules.append(Rule(pattern, _text(entry.get("reason"), f"{where}.reason")))
    return tuple(rules)


def _presets(data: dict) -> tuple[str, ...]:
    names = data.get("presets", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ConfigError("presets must be a list of names")
    for index, name in enumerate(names):
        if name not in PRESETS:
            known = ", ".join(sorted(PRESETS))
            raise ConfigError(f"unknown preset: {name} (known: {known})")
        if name in names[:index]:
            raise ConfigError(f"preset listed twice: {name}")
    return tuple(names)


def _with_presets(own: tuple[Rule, ...], names: tuple[str, ...]) -> tuple[Rule, ...]:
    """The adopter's own rules first, so their reason wins; then each preset's,
    in the listed order, minus any pattern already present."""
    rules = list(own)
    seen = {rule.pattern for rule in own}
    for name in names:
        for rule in PRESETS[name]:
            if rule.pattern not in seen:
                seen.add(rule.pattern)
                rules.append(Rule(rule.pattern, f"{rule.reason}; preset {name}"))
    return tuple(rules)


def _base_events(data: dict, warnings: list[str]) -> tuple[str, ...]:
    """The events whose runs decide the base branch's colour; empty means all."""
    if "base_events" not in data:
        return ()
    events = data["base_events"]
    if not isinstance(events, list) or not events:
        raise ConfigError("base_events must be a non-empty list of event names")
    names = tuple(_text(event, "base_events entry") for event in events)
    for name in names:
        if name not in WORKFLOW_EVENTS:
            warnings.append(
                f"base_events: {name} is not a GitHub Actions event this version "
                "knows; check the spelling, or runs of it will count nothing"
            )
        if name in PULL_REQUEST_EVENTS:
            raise ConfigError(f"base_events: {name} runs never test the base branch")
    if "workflow_dispatch" not in names:
        # The bot tests the base by dispatching; uncounted, its own runs would
        # decide nothing and each pass would dispatch again.
        raise ConfigError(
            "base_events must include workflow_dispatch, the runs the bot starts"
        )
    return names


def parse(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"not valid TOML: {exc}") from exc
    for key in REQUIRED:
        if key not in data:
            raise ConfigError(f"missing required key: {key}")
    unknown = sorted(set(data) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        raise ConfigError(f"unknown key: {unknown[0]}")
    method = data.get("merge_method", "merge")
    if method not in MERGE_METHODS:
        raise ConfigError(f"merge_method must be one of {', '.join(MERGE_METHODS)}")
    dry_run = data.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ConfigError("dry_run must be true or false")
    on_red_base = data.get("on_red_base", "stop")
    if on_red_base not in ON_RED_BASE:
        raise ConfigError(f"on_red_base must be one of {', '.join(ON_RED_BASE)}")
    warnings: list[str] = []
    base_events = _base_events(data, warnings)
    checks = data.get("required_checks", [])
    if not isinstance(checks, list):
        raise ConfigError("required_checks must be a list of names")
    presets = _presets(data)
    return Config(
        ready_label=_text(data["ready_label"], "ready_label"),
        base_branch=_text(data["base_branch"], "base_branch"),
        ci_workflow=_text(data["ci_workflow"], "ci_workflow"),
        merge_method=method,
        dry_run=dry_run,
        on_red_base=on_red_base,
        base_events=base_events,
        required_checks=tuple(_text(c, "required_checks entry") for c in checks),
        shared_risk=_with_presets(_rules(data, "shared_risk"), presets),
        added_together=_rules(data, "added_together"),
        presets=presets,
        warnings=tuple(warnings),
    )


def load(path: Path) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    return parse(text)
