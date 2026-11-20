# make-magic

This repo is a public, GPL-3.0-or-later Claude Code plugin marketplace for Magic: The Gathering. The substantive code is the make-magic plugin, whose Python package `make-magic-pipeline` lives under `plugins/make-magic/pipeline/`. It builds decks, chases cards, manages inventory, and runs rules-enforced AI-vs-AI simulations through MTG Forge. It works out of the box with no account: card data comes from Scryfall, and a local store holds your collection by default.

This file is the one always-on contract. Read it first. Everything the repo map names is on disk today; the Planned section names what a later phase adds. `scripts/check_context.py` keeps both honest and fails the build when a mapped path goes missing or a planned path quietly ships.

## Repo map

| Path | What it is |
|---|---|
| `AGENTS.md` | this file: the always-on agent contract |
| `.claude/rules/` | path-scoped must and never constraints that Claude loads on a matching file |
| `.claude/rules/pipeline.md` | constraints for the Python package |
| `.claude/rules/changes.md` | constraints for workflow and CI edits and the git flow |
| `.claude/skills/mm-change/SKILL.md` | the branch, check, commit, PR, and merge procedure |
| `.claude/settings.json` | the marketplace reference this repo enables |
| `scripts/check` | the one check that local, CI, and the pre-commit hook all run |
| `scripts/install-hooks` | installs the pre-commit hook that runs the check |
| `scripts/check_context.py` | the linter that keeps this file honest against the tree |
| `scripts/check_pr.py` | the PR-body and attribution-footer validator |
| `scripts/check_versions.py` | the plugin-version lockstep linter the check runs |
| `scripts/apply-ruleset` | applies the main-branch ruleset through gh api and reads it back |
| `scripts/merge-stacked` | squash-merges a native stacked PR through merge-async and polls until it lands |
| `.github/pull_request_template.md` | the PR body template the validator checks |
| `.github/workflows/ci.yml` | the offline gate plus the PR-body, repro, and ci-ok jobs |
| `.github/workflows/release.yml` | cuts the GitHub Release on a `v<x.y.z>` tag push |
| `.github/workflows/make-magic-repro.yml` | the reproducibility gate, called on a release PR |
| `.github/workflows/upstream-canary.yml` | the weekly check that runtime-fetched deps still resolve |
| `.github/workflows/forge-simai-build.yml` | the Forge sim-AI harness jar bytecode check |
| `.github/workflows/xmage-dist-release.yml` | the XMage dist build and harness jar check |
| `.github/workflows/deploy-website.yml` | the website build and deploy |
| `plugins/make-magic/` | the plugin: skills, hooks, the Airtable MCP, and the pipeline package |
| `plugins/make-magic/pipeline/` | the Python package `make-magic-pipeline` and its test suite |
| `plugins/make-magic/skills/` | the user-facing deck-building skills |
| `plugins/make-magic/scripts/` | the plugin CLIs: the collection and simulate entrypoints |
| `plugins/make-magic/mcp/` | the vendored Airtable MCP server, opt-in |
| `app/` | the deck-diff viewer, an Astro app |
| `website/` | the marketing site, an Astro app |
| `.claude-plugin/marketplace.json` | the marketplace manifest |

## Commands

Run the `uv run` commands from `plugins/make-magic/pipeline`.

| Command | Does |
|---|---|
| `./scripts/check` | the one gate: ruff, pyright, pytest, markdown lint, then the context linter |
| `./scripts/install-hooks` | installs the pre-commit hook that runs the gate |
| `uv run --extra dev pytest` | the offline test suite, no network and no Forge |
| `uv run --extra dev ruff check` | lint |
| `uv run --extra dev ruff format --check` | format check |
| `uv run --extra dev pyright` | type check |
| `uv run --extra dev pytest -m forge` | the gated real-Forge games, needs a Forge install |
| `uv run --extra dev pytest -m live` | the gated real-Airtable tests, need a key |
| `uv run --extra dev pytest -m canary` | the gated upstream-resolve checks |

## How a change reaches main

Work is trunk-based off `main`. The skill at `.claude/skills/mm-change/SKILL.md` holds the full procedure. In short:

- branch `<type>/<slug>` off `main`. the type is one of feat, fix, chore, docs, ci, test, refactor, release. never push to `main`, because the branch and PR are the only path in.
- write Conventional Commits. the PR title becomes the squash subject on `main`.
- the pre-commit hook runs `./scripts/check --fast` (ruff, format, markdown, version lockstep, context linter) on commit, so commits stay quick. run the full `./scripts/check` before you push, because CI runs the full gate and a fast pass is not a CI pass.
- open a PR and fill the template. the PR body references an issue with `Closes #N` or `Part of #N` and ends with the attribution footer. the template is `.github/pull_request_template.md` and `scripts/check_pr.py` validates the body in CI.
- merge is squash-only with linear history, and the branch deletes on merge. the author never merges; a separate reviewer merges on a green gate, because a second person is the last check before `main`.
- the synthesized `ci-ok` status is the one required check. it fans in over the `check`, `pr-body`, and `repro` jobs and passes when each is success or skipped. `repro` runs only on a `release/*` PR and is skipped (a pass) elsewhere.

### Releases

A release is a version-bump PR, not a special ceremony. On a `release/<x.y.z>` branch, bump both version files to the same version (`scripts/check_versions.py` enforces the lockstep), fill the template, and let it clear `ci-ok` like any PR — on a `release/*` PR the `repro` reproducibility gate runs and must pass. A reviewer squash-merges; then a human pushes the `v<x.y.z>` tag (tags are not ruleset-protected, so the tag push is a deliberate human-only act), which fires `.github/workflows/release.yml` to verify the lockstep against the tag and cut the GitHub Release. The version bump follows the pre-1.0 policy: `fix`/`chore`/`ci`/`docs` → patch, `feat` → minor, and a breaking change → minor (major is reserved for `1.0.0`).

## Safety

- never write a secret value into a file, a command line, a log, a commit message, or PR text. read it from stdin or a 0600 file, because every clone of the repo holds the file.
- never degrade the local-first, no-credential default path. make-magic runs with no account, and that is its core promise. keep the local store the default and Airtable opt-in through `AIRTABLE_API_KEY` in the environment.
- never commit an Airtable key or any credential. the opt-in base reads its key from the environment, not from a tracked file.

## Agent context

- rules in `.claude/rules/` load when you touch a matching file: `.claude/rules/pipeline.md` for `plugins/make-magic/**`, and `.claude/rules/changes.md` for `.github/**`.
- the skill at `.claude/skills/mm-change/SKILL.md` holds the change procedure. constraints belong in the rules, not the skill.
- after any edit under `.claude/` or to this file, run `scripts/check` and fix every finding.

## Planned

Later phases bolt on the untrusted-contributor governance, modeled on the rae-labs exemplar and reserved here, not built. Each path moves into the repo map in the same change that creates it:

- `.github/areas.json`: the reviewer auto-assign map. phase 2.

The governance itself, each modeled on the exemplar: DCO sign-off on commits, reviewer auto-assign, fork-safe handling of outside pull requests, offline semgrep threat-model rules, and a plain-prose linter for the agent files.
