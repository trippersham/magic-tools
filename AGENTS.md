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
| `.github/workflows/ci.yml` | the offline lint, type, and test gate |
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

- branch `<type>/<slug>` off `main`. the type is one of feat, fix, chore, docs, ci, test, refactor. never push to `main`, because the branch and PR are the only path in.
- write Conventional Commits. the PR title becomes the squash subject on `main`.
- the pre-commit hook runs `./scripts/check --fast` (ruff, format, markdown, context linter) on commit, so commits stay quick. run the full `./scripts/check` before you push, because CI runs the full gate and a fast pass is not a CI pass.
- open a PR and fill the template. the PR body references an issue with `Closes #N` or `Part of #N` and ends with the attribution footer. the template and its validator arrive in phase 3.
- merge is squash-only with linear history, and the branch deletes on merge. the author never merges; a separate reviewer merges on a green gate, because a second person is the last check before `main`.
- the synthesized `ci-ok` status is the one required check. it arrives in phase 3; until it lands, the `check` job is the gate.

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

- `.github/pull_request_template.md` and `scripts/check_pr.py`: the PR template and the validator that rejects a PR whose body is missing its issue link, filled template, or attribution footer. phase 3.
- `scripts/apply-ruleset`: the main-branch ruleset applied through gh api. phase 5.
- `.github/areas.json`: the reviewer auto-assign map. phase 2.

The governance itself, each modeled on the exemplar: DCO sign-off on commits, reviewer auto-assign, fork-safe handling of outside pull requests, offline semgrep threat-model rules, and a plain-prose linter for the agent files.
