# Carl Mergé: technical guide

How Carl decides, how to adopt it, and every setting. For what Carl is and
why it exists, see the [README](../README.md).

## 1. How it decides, exactly

For each pull request that carries the ready label, oldest first and **one at a
time** (a single concurrency group, never parallel), it acts on the first row
that fits:

| Pull request state | Action |
|---|---|
| Conflicting | Comment the reason and remove the ready label, so whoever owns the branch can fix it |
| Up to date, required checks green | Merge, with the head pinned |
| Behind, the overlap check says `disjoint` | Re-read head and base, then bypass-merge with the head pinned. No branch update and no CI run |
| Behind, the check says `wait` | Update the branch (pinned to the head it saw), then merge when the required checks are green on the new head. A conflict from the update goes to the first row |
| The check cannot decide | Comment why, keep the label, leave it for the owner |
| The base branch is red (section 5) | With `on_red_base = "stop"` (the default): merge nothing until it is green again, and comment on each queued pull request with a link to the red run. With `"merge-tested"`: see below |

A draft, a fork, and a pull request whose required checks are red or unknown
all count as "cannot decide". A pull request whose mergeability GitHub has not
worked out yet is skipped without a comment, and the next pass looks again.

**On a red base.** With the default `on_red_base = "stop"` nothing merges, so
the pull request that fixes the base waits for a human. With
`on_red_base = "merge-tested"` the bot still reads the queue, and a conflicting
pull request or one it cannot decide is handled as usual. The merge, bypass and
update rows give way to one rule: it merges a pull request only when it is not
behind, its required checks are green, and they ran against the base head
itself: a plain merge of exactly the tree its checks passed on, never a bypass
and never an update. Every other pull request is skipped with a comment that
says why, and the pass moves on to the next one, so a younger fix can go ahead
of older pull requests. After one such merge the pass ends, since the base
moved and no other pull request's checks ran on the new head, and the bot
dispatches `ci_workflow` on the base. Turn it on only when the required checks
are as strong as `ci_workflow`; where they are lint-only, it stacks untested
changes on a broken base.

Either way, each red-base comment links the red run and is posted once per
head and red run. When the red run is a dispatched run, it tested every merge
since the counted run before it, so each pull request the bot bypass-merged in
that range also gets one comment per red run linking it, since one of them is
the likely cause. It reads back at most 30 commits along the base branch's
own history, so in a longer range only the newest 30 merges are checked.

**Two windows, two guards.** The update is pinned with `expected_head_sha`, so
GitHub refuses it if someone pushed meanwhile. The merge is guarded by
re-reading the head and the base immediately before it: a head that moved
aborts any merge, and a base that moved aborts a bypass merge. Both guards are
required, and the merge itself also carries the head it expects.

**The overlap check.** A behind pull request is `disjoint` when none of these
holds, comparing the files it changed with the files the base gained since its
checks ran:

- a file changed on both sides;
- a path matching a `shared_risk` rule changed on either side;
- both sides **added** a file matching the same `added_together` rule in the
  same directory.

"Since its checks ran" means since the tested base: the base that the required
checks' own Actions runs were tested against. When those runs do not all agree
on one base, or a required check is not an Actions run, there is nothing safe to
compare against, so the pull request takes the update row instead.

After each merge the bot comments on the pull request. Each comment ends with a
hidden marker, and the bot posts each comment once per head.

## 2. Adopting it

1. Download the wheel from PyPI and commit it to your repository:

   ```bash
   python3 -m pip download carl-merge==0.1.1 --no-deps --only-binary :all: -d vendor/
   ```

   That saves `vendor/carl_merge-0.1.1-py3-none-any.whl`.

   The workflow puts the wheel on `PYTHONPATH`. A pure Python wheel imports
   from the zip, so the runner needs no `pip install`.
2. Add `.github/carl-merge.toml`. Start from `examples/carl-merge.toml`.
3. Add `.github/workflows/carl-merge.yml`. Start from `examples/carl-merge.yml`.
4. Create and install the App (section 3) and set up the ruleset (section 4).

A label added by a job using its own `GITHUB_TOKEN` starts no workflow run. If
a workflow applies your ready label, list that workflow under `workflow_run`
in the example, so the bot runs when the labelling finishes.

The job checks out the default branch and never runs a pull request's code. It
only fetches pull request heads as git objects to run `git diff`, which is why
`pull_request_target` is safe here.

## 3. The GitHub App

The bot authenticates as a GitHub App **installation** token, minted in the
workflow by `actions/create-github-app-token`. The bot never sees the private
key and the App needs no webhook. The bot reads only `CARL_MERGE_TOKEN`, and it
never uses the job's `GITHUB_TOKEN`: a merge done with that token would be
attributed to the wrong actor and could not use the App's bypass.

Every repository owner creates an App of their own. Nobody else ever holds
its private key.

### The quick way: `create-app`

With [`uv`](https://github.com/astral-sh/uv) installed and
[`gh`](https://github.com/cli/cli) signed in with the right to set secrets on
the repository:

```bash
uvx carl-merge create-app --repo OWNER/NAME
```

`uvx` fetches Carl from PyPI and runs it once, without installing it. Without
`uv`, run it from the committed wheel (section 2) instead:

```bash
PYTHONPATH=vendor/carl_merge-0.1.1-py3-none-any.whl \
  python3 -m carl_merge create-app --repo OWNER/NAME
```

1. Your browser opens GitHub's "Create GitHub App" page, filled in from a
   manifest: the permissions below, no webhook, private to your account or
   organisation.
2. Check the name and click **Create GitHub App**. App names are unique across
   GitHub, so the default is `Carl Mergé OWNER`. Pass `--name` to choose
   another.
3. The command stores the client ID as the variable `CARL_MERGE_CLIENT_ID` and
   the private key as the secret `CARL_MERGE_PRIVATE_KEY`. The key goes to
   `gh` on stdin and is never printed. If `gh` cannot store it, the command
   writes it to a file only you can read and tells you what to do with it.
4. It saves `carl-merge-logo.png` in the directory you ran it from and opens
   the App's settings page. Click **Upload a logo** under **Display
   information** and pick that file. GitHub has no way to set a logo for you.
5. Do the two steps it prints after that:
   - install the App on the repository;
   - add the App to the ruleset's bypass list (section 4).

The page the browser opens first is served on `127.0.0.1` by the command
itself, only until GitHub sends you back. Nothing is hosted anywhere else.

### By hand

Create a GitHub App with these repository permissions and no webhook, install
it on the repository, and store the client ID as the variable
`CARL_MERGE_CLIENT_ID` and the private key as the secret
`CARL_MERGE_PRIVATE_KEY`:

| Permission | Access | Used for |
|---|---|---|
| Contents | read and write | updating the branch |
| Pull requests | read and write | reading, merging |
| Issues | read and write | comments and labels |
| Actions | read and write | reading runs, cancelling stale ones, dispatching |
| Checks | read | required check runs |
| Commit statuses | read | required status contexts |
| Workflows | read and write | a branch update or merge that changes workflow files |
| Metadata | read | always granted |

## 4. The ruleset

A bypass merge needs the App on the base branch ruleset's bypass list. Add it
as an `Integration` actor in **`pull_request`** mode:

- `pull_request` lets the App bypass the rules only when it merges a pull
  request, which is the only thing it should ever do, and it leaves a bypass
  entry in the audit log.
- Never `exempt`: no rules run at all for it, and no bypass **audit** entry is
  written, so nobody can see afterwards what it skipped.
- Never `always`: the App could then **push** to the branch outside a pull
  request, which nothing in this bot needs.

The bot sets the bypass flag on exactly one row: the `disjoint` row. Every other
merge asks GitHub to enforce all the rules.

## 5. What it cannot see

The check is **file-level, not semantic**. Two pull requests that touch
different files can still break each other: one renames a function the other
calls, one changes a setting the other relies on. The bot cannot see that.
There are two backstops:

1. After a pass that bypass-merged at least one pull request, it **dispatches**
   the configured full workflow (`ci_workflow`) on the base branch once, when
   the pass ends. That run tests the combined result. GitHub can finish a
   bypass merge after the bot has stopped waiting for it, so the bot labels the
   pull request `carl-merge-ci-owed` before it asks for the merge, creating
   the label if it is missing, and removes it only after that dispatch
   succeeds. When the merge lands later, or the dispatch fails, the first live
   pass that finds the pull request merged dispatches once for it. The
   landing's own push to the base branch starts that pass.
2. While the base branch is **red**, it merges nothing, or with
   `on_red_base = "merge-tested"` only the pull requests whose checks ran on
   the base head (section 1). The colour comes from the completed runs of that
   workflow on the base branch that count: those whose event is in
   `base_events` (by default every event but `pull_request` and
   `pull_request_target`), and that are not cancelled, skipped or neutral. The
   commit of the newest counted run decides, and on that commit a red run
   outranks a green one, so a quick lint run cannot hide a red full run on the
   same commit, whichever started first. A branch with no run at all counts as
   not red. If the first 500 completed runs hold none that counts, the pass
   stops with an error rather than guess; if they all sit on one commit with
   no red run among them, that commit counts as green.

   When the base reads red but its head has moved past the red run (a merge by
   hand, or a merge on a red base) and no counted run is queued or running on
   the new head, the bot dispatches `ci_workflow` on the base once, so the new
   head gets a verdict of its own. Before it does, it waits a few seconds and
   looks again, since the push that moved the base usually starts a counted
   run of its own; when one has started, it does not dispatch.

   What the bot cannot see is whether a counted run ran your full suite. If the
   workflow skips its tests on `push`, a green push run on a commit nobody
   tested still turns the base green. Set `base_events` to the events whose
   runs are full, such as `["workflow_dispatch", "schedule"]`. The list must
   include `workflow_dispatch`: the bot tests the base by dispatching, and runs
   it cannot count would never turn the base green. While the newest full run
   is still going, the previous one decides.

Add `shared_risk` and `added_together` rules for the places where your project
couples files that the diff cannot show, such as lockfiles, migrations and
settings.

## 6. Configuration reference

`.github/carl-merge.toml`. An unknown key, a wrong type, or an empty pattern or
reason stops the run with exit code 2.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `ready_label` | string | required | The label that puts a pull request in the queue |
| `base_branch` | string | required | The branch pull requests merge into |
| `ci_workflow` | string | required | The workflow file to dispatch after a bypass, and to read for red |
| `merge_method` | `"merge"` or `"squash"` | `"merge"` | How a pull request is merged |
| `dry_run` | boolean | `true` | Post what it would do and change nothing |
| `on_red_base` | `"stop"` or `"merge-tested"` | `"stop"` | On a red base, merge nothing, or merge only up-to-date pull requests whose required checks ran on the base head (section 1) |
| `base_events` | list of strings | every event but pull requests | The events whose `ci_workflow` runs decide whether the base is red (section 5); must include `workflow_dispatch`; `pull_request` and `pull_request_target` are refused, and an event name this version does not know gets a warning |
| `required_checks` | list of strings | `[]` | Check names that must be green; empty reads the base branch rules |
| `[[shared_risk]]` | tables | none | One-sided rules: `pattern` and `reason` |
| `[[added_together]]` | tables | none | Two-sided rules: `pattern` and `reason` |

Patterns are paths relative to the repository root. `**/name` matches a file
at any depth, `dir/**` matches everything below a directory, and anything else
is a shell glob (`*` also crosses `/`). Either list may be empty, and then only
a file changed on both sides blocks a bypass.

From the made-up shop project in `examples/carl-merge.toml`:

```toml
ready_label = "ready-to-merge"
base_branch = "main"
ci_workflow = "ci.yml"
dry_run = true

[[shared_risk]]
pattern = "pyproject.toml"
reason = "dependencies"

[[added_together]]
pattern = "*/migrations/*.py"
reason = "migration numbers"
```

With that file, two pull requests that each add a file under `shop/migrations/`
wait for each other, two that add migrations in different apps do not, and
neither does one that only edits an existing migration.

The command line is `python3 -m carl_merge run [--config PATH] [--dry-run]`.
It reads `CARL_MERGE_TOKEN`, `CARL_MERGE_APP_SLUG` and `GITHUB_REPOSITORY`, and
prints one line per action. Exit code 0 means a pass ran, 2 a setup error, and
3 a failure that is not one pull request's (401, 403, 5xx, a rate limit); a pass
that had already bypass-merged still dispatches before it exits.

## 7. Dry run first

`dry_run = true` is the default. In dry run the bot merges, updates, relabels,
cancels and dispatches nothing. It posts a comment starting `Dry run:` on each
pull request saying what it would do, once per head. Label a few pull requests,
read one batch of those comments, and when they match what you would have done,
set `dry_run = false`.

## 8. Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
```
