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

The type is one of feat, fix, chore, docs, ci, test, refactor. The slug is two to four words joined by hyphens, such as `feat/strategy-overlay`. When your task builds on an open PR that is not merged, branch from that branch instead of `origin/main`.

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
