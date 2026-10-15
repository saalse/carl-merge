"""`carl-merge run` makes one pass; `carl-merge create-app` registers the App."""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

from carl_merge.config import Config, ConfigError, load
from carl_merge.create_app import CreateAppError, create_app
from carl_merge.forge import ForgeError, Pull
from carl_merge.gitdiff import compare
from carl_merge.overlap import Decision
from carl_merge.queue import run_pass
from carl_merge.rest import RestForge
from carl_merge.transport import Transport

DEFAULT_CONFIG = ".github/carl-merge.toml"
ENV = ("CARL_MERGE_TOKEN", "CARL_MERGE_APP_SLUG", "GITHUB_REPOSITORY")


def _overlap(config: Config):
    def decide(pull: Pull, base_sha: str, tested_base: str | None) -> Decision:
        return compare(
            Path.cwd(), pull.head_sha, base_sha, tested_base, config, number=pull.number
        ).decision

    return decide


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carl-merge")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="make one pass over the ready queue")
    run.add_argument("--config", type=Path, default=Path(DEFAULT_CONFIG))
    run.add_argument("--dry-run", action="store_true", help="force dry run")
    new = commands.add_parser("create-app", help="register the GitHub App")
    new.add_argument("--repo", required=True, help="OWNER/NAME to set it up for")
    new.add_argument("--name", help="the App's name, unique across GitHub")
    args = parser.parse_args(argv)

    if args.command == "create-app":
        try:
            create_app(args.repo, args.name)
        except CreateAppError as exc:
            print(f"carl-merge: {exc}", file=sys.stderr)
            return 3
        return 0

    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"carl-merge: {exc}", file=sys.stderr)
        return 2
    for warning in config.warnings:
        print(f"carl-merge: warning: {warning}", file=sys.stderr)
    missing = [name for name in ENV if not os.environ.get(name)]
    if missing:
        print(f"carl-merge: set {', '.join(missing)}", file=sys.stderr)
        return 2
    if args.dry_run:
        config = dataclasses.replace(config, dry_run=True)

    forge = RestForge(
        Transport(os.environ["CARL_MERGE_TOKEN"]),
        repo=os.environ["GITHUB_REPOSITORY"],
        app_slug=os.environ["CARL_MERGE_APP_SLUG"],
        base_branch=config.base_branch,
        required_checks=config.required_checks,
        base_events=config.base_events,
    )
    try:
        run_pass(forge, config, _overlap(config), log=print)
    except ForgeError as exc:
        print(f"carl-merge: {exc}", file=sys.stderr)
        return 3
    return 0
