# Contributing to magic-tools

Thanks for your interest! This repo is the **magic-tools** Claude Code plugin
marketplace; the substantive code lives in the **make-magic** plugin
(`plugins/make-magic/`), whose Python package is under
`plugins/make-magic/pipeline/`.

## License

This project is licensed **GPL-3.0-or-later** (see [LICENSE](LICENSE)). By
contributing, you agree that your contributions are licensed under the same terms.

## Prerequisites

- **macOS or Linux.**
- **[uv](https://docs.astral.sh/uv/)** — the plugin self-provisions a pinned copy at
  runtime, but for local development install it yourself.
- **Java is NOT required to develop** — the offline test suite mocks Forge. Running
  the *gated* `-m forge` tests needs a real Forge install (or lets make-magic fetch
  one; see below).

## Setup & the test suite

```bash
cd plugins/make-magic/pipeline

uv run --extra dev pytest              # offline suite — fast, no network, no Forge
uv run --extra dev ruff check          # lint
uv run --extra dev ruff format --check # formatting
uv run --extra dev pyright             # type-check
```

All four must pass for a change to land. Please add tests for new behavior
(the suite is TDD-oriented; parsing changes get fixtures, integrity changes get
round-trip tests).

### Gated markers

Two pytest markers are deselected by default (they need external resources):

- `-m live` — exercises a real Airtable base (needs `AIRTABLE_API_KEY`).
- `-m forge` — runs **real** headless MTG Forge games. Needs a Forge install; set
  `MAKE_MAGIC_FORGE_HOME` + `MAKE_MAGIC_JAVA`, or let `scripts/simulate doctor
  --provision` fetch Forge + a JRE first. These spawn JVMs — the concurrency
  governor caps the pool, but run them deliberately.

```bash
uv run --extra dev pytest -m forge     # only when you have Forge available
```

## Conventions

- **Style:** ruff (single quotes; see `pyproject.toml`). Keep the codebase's
  high docstring density — especially the *why* behind empirically-derived Forge
  constants.
- **Commits:** conventional-commit style (`feat(sim): …`, `fix(collection): …`).
- **Scope:** the pipeline is *additive* to the collection/deck-building workflow and
  must never degrade the local-first, no-credential default path.

## Reporting issues

Use the [issue tracker](https://github.com/trippersham/magic-tools/issues). For a
simulation bug, include the `scripts/simulate doctor` output and, where relevant,
the stored game log (`scripts/simulate log …`).

This project is unofficial Fan Content and is not affiliated with Wizards of the
Coast.
