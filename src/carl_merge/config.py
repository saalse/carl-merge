"""One TOML file in the adopting repository configures the bot."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

REQUIRED = ("ready_label", "base_branch", "ci_workflow")
OPTIONAL = (
    "merge_method",
    "dry_run",
    "required_checks",
    "shared_risk",
    "added_together",
)
MERGE_METHODS = ("merge", "squash")


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
    required_checks: tuple[str, ...] = ()
    shared_risk: tuple[Rule, ...] = ()
    added_together: tuple[Rule, ...] = ()


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
        rules.append(
            Rule(
                _text(entry.get("pattern"), f"{where}.pattern"),
                _text(entry.get("reason"), f"{where}.reason"),
            )
        )
    return tuple(rules)


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
    checks = data.get("required_checks", [])
    if not isinstance(checks, list):
        raise ConfigError("required_checks must be a list of names")
    return Config(
        ready_label=_text(data["ready_label"], "ready_label"),
        base_branch=_text(data["base_branch"], "base_branch"),
        ci_workflow=_text(data["ci_workflow"], "ci_workflow"),
        merge_method=method,
        dry_run=dry_run,
        required_checks=tuple(_text(c, "required_checks entry") for c in checks),
        shared_risk=_rules(data, "shared_risk"),
        added_together=_rules(data, "added_together"),
    )


def load(path: Path) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    return parse(text)
