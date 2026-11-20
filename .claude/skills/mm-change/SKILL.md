---
name: mm-change
description: Use for ANY change in make-magic that must reach main. Covers starting a task, creating the branch, running the check gate, committing, pushing, opening the pull request, reading CI, answering review, and finishing. Use whenever the task says "implement", "make the change", "commit and push", "open a PR", or "ship this", or before you run git commit, git push, or gh pr create.
---
# Change workflow: branch, check, commit, PR

This skill takes a change from a clean `main` to a reviewed, merged PR. It is the procedure. The standing constraints live in `.claude/rules/changes.md` and `.claude/rules/pipeline.md`; read them when you touch a matching file.

## Primary constraint

Never push to `main`, and never force-push a branch another person has pulled. The author never merges: a separate reviewer merges a PR on a green gate, after checking the head commit. An agent that wrote the change reports the PR and stops.

## Before you start

- `git status --porcelain` prints nothing.
- `git fetch origin` is done, and your branch starts from `origin/main`.
- `gh auth status` succeeds.
- the task names what it changes and how to prove it.

## Steps

| Step | Operation | When |
|---|---|---|
| 1 | start a task | the beginning |
| 2 | work | writing code and tests |
| 3 | run the gate | before the commit |
| 4 | commit | the change is ready |
| 5 | push and open the PR | the commit is done |
| 6 | answer CI and review | the PR is open |
| 7 | finish | CI is green and review is done |

### 1. Start a task

```bash
git switch -c <type>/<slug> origin/main
```

The type is one of feat, fix, chore, docs, ci, test, refactor, release. The slug is two to four words joined by hyphens, such as `feat/strategy-overlay`. A release branch is named for its version, `release/x.y.z`, and its PR bumps both version files to that version. When your task builds on an open PR that is not merged, branch from that branch instead of `origin/main`.

### 2. Work

Write the test first, then the code. Add a test for new behavior, as `.claude/rules/pipeline.md` requires. Run the package tools from the package directory:

```bash
cd plugins/make-magic/pipeline
uv run --extra dev pytest
```

### 3. Run the gate

Run the full gate from the repo root before you push:

```bash
./scripts/check
```

It runs ruff, pyright, pytest, the markdown lint of the agent files, and the context linter, which is exactly what CI runs. Fix every finding. The pre-commit hook runs only the fast subset (`./scripts/check --fast`: ruff, format, markdown lint, context linter, no pyright or tests) so commits stay quick; run the full `./scripts/check` yourself before you push. When you added or removed a path named in `AGENTS.md`, move it between the repo map and the Planned section in the same change, so the context linter stays green.

### 4. Commit

Stage by path after you read the status, never `git add -A` at the root:

```bash
git add -- <paths>
git commit
```

Write a Conventional Commits subject, `type: subject` or `type(scope): subject`, of about 80 characters, and a body that says what changed and why. More than one commit per PR is fine. The pre-commit hook runs the fast gate (`./scripts/check --fast`) on commit; never pass `--no-verify`.

### 5. Push and open the PR

```bash
git push -u origin HEAD
gh pr create --base main --title "<commit subject>" --body-file <file>
```

The PR title becomes the squash subject on `main`. Fill the PR body from the template: reference an issue with `Closes #N` or `Part of #N`, and end with the attribution footer. Edit a title or body later with `gh pr edit`.

### 6. Answer CI and review

Poll the checks until none is pending:

```bash
gh pr checks <n>
```

Do not watch in the foreground, because it blocks past the command timeout. On a red check, read the failing log, fix the cause, and push a new commit. Never skip or weaken a check. When the failure shows a wrong plan fact or needs a write you were not asked for, stop and report it. Answer each review comment with a commit and a one-line reply that says what changed.

### 7. Finish

Report the PR URL, the proof, and the review state. Do not merge, and do not delete the branch. A reviewer merges on a green gate.

## Stacked PRs with `gh stack`

Default to one small independent PR branched off fresh `origin/main`. It reviews and lands on its own, so prefer splitting work that way. Reach for a stack **only when a change genuinely depends on another unmerged change** — then the second branch must build on the first, and a stack keeps each PR small and separately reviewable.

Stacking uses the official `github/gh-stack` extension; there is no `flow` command. The verbs:

| Verb | Does |
|---|---|
| `gh stack init` | start tracking the current branch as the bottom of a stack |
| `gh stack add` | add the current branch onto the stack, based on the one below it |
| `gh stack submit` | open or update the PRs for the stack, each based on the branch below it |
| `gh stack view` | show the stack; `gh stack view --json` exposes a per-branch `⚠ Needs rebase` signal |
| `gh stack sync --prune` | fast-forward trunk, cascade-rebase children, force-with-lease push, prune merged branches |

Two facts to work with, both established empirically:

- `gh stack submit --auto` opens the PRs as **drafts**, and the async merge refuses a draft. Submit without `--auto`, or run `gh pr ready <n>`, before a stacked PR can merge.
- Merging the bottom PR **auto-retargets and rebases the rest of the stack server-side** — even when a child edited the same line a squashed parent did. You do not hand-rebase children after a parent merges; GitHub does it. A local working copy then only needs `gh stack sync --prune` to catch up.

## Merging (reviewer only)

The author never merges; this section is for the reviewer who merges on a green gate. Pick the command by the PR's shape:

- **Ordinary PR** (based on `main`): `gh pr merge <n> --squash --delete-branch`.
- **Native stacked PR** (opened by `gh stack submit`): `scripts/merge-stacked <n>`. A stacked PR **cannot** be merged with `gh pr merge` or the ordinary REST merge — both 403 with "Use the asynchronous merge endpoint." `scripts/merge-stacked` squash-merges through `merge-async` and polls until the PR is actually MERGED, failing loud if the enqueue never lands (for example when `ci-ok` is red). "Enqueued" is not "merged".

In both cases merge only on a green `ci-ok`. For a stacked PR, merge the bottom of the stack first; GitHub restacks the rest server-side.

## Red flags

| Bad instinct | Do this instead |
|---|---|
| "I will push to main to save a step" | branch and open a PR |
| "the checks are green, I will merge" | the author never merges; a reviewer does |
| "I will add `--no-verify`" | fix the hook failure |
| "`git add -A` is faster" | stage by path after `git status` |
| "I will relax the check so CI passes" | fix the cause, or stop and report |
| "I will paste the token on the command line" | read it from stdin or a 0600 file |
| "this PR is docs only, skip the gate" | run `./scripts/check` anyway |
