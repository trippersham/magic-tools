---
paths:
  - ".github/**"
---
# Workflows, CI, and the git flow

These files control CI and the way a change reaches `main`. A wrong workflow can leak a secret, skip the gate, or let an unreviewed commit land.

- never push to `main`. one task is one branch is one PR, and the branch and PR are the only path in. reason: the gate and the review run on the PR, not on a direct push.
- the author never merges. a separate reviewer merges on a green gate, because a second person is the last check before `main`. an agent that wrote the change reports the PR and stops.
- never weaken, skip, or add `--no-verify` to the gate to make a red check pass. fix the cause, or stop and report it. reason: the gate is the contract, and a disabled check hides the regression it would have caught.
- merge squash-only with linear history, and let the branch delete on merge. the PR title becomes the squash subject on `main`, so write it as a Conventional Commits subject.
- the PR body must reference an issue with `Closes #N` or `Part of #N` and end with the attribution footer `Posted on behalf of <name> by <tool and model>`. reason: the squash subject and body stay on `main` for good, and the footer records who ran the change.
- never remove a job from the `needs` list of the synthesized `ci-ok` merge check, and add every new job to it. reason: a job outside that list cannot block a merge, and a skipped required check counts as a pass.
- never add `pull_request_target` to a workflow. reason: it runs fork code with the repo secrets. the fork-safe governance is reserved for phase 2.
- never print a secret or pass one on a command line in a workflow. read it from a repo secret or a file, because the run log is public on a public repo.
- keep the existing release, canary, and harness-jar workflows working when you edit CI. they verify the runtime-fetched pins and the harness bytecode, and a merge that breaks one ships a broken release.
- keep the plugin-version lockstep enforced at both gates. `scripts/check` asserts `plugin.json` equals `pyproject.toml` on every PR (inside `ci-ok`), and `release.yml` re-asserts both against the pushed tag before it cuts a Release, so a mistagged or divergent release cannot ship.
