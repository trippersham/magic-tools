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

## Cutting a release

Versions are declared in **more than one file**; a partial bump ships a broken install.
`tests/test_versioning.py` runs in the normal CI suite and turns any drift below into a
red build — so keep these in lockstep.

**Bumping the plugin / marketplace version** — update *both*, to the same value:

- `plugins/make-magic/.claude-plugin/plugin.json` → `version`
- `plugins/make-magic/pipeline/pyproject.toml` → `project.version`

(`.claude-plugin/marketplace.json` carries no version — it references the plugin by
path, so there's nothing to bump there.)

**Bumping a vendored install** — these are fetched at runtime and pinned by SHA:

- **Forge** (`pipeline/sim/forge_runtime.py`): bump `FORGE_VERSION` **and** re-pin
  `FORGE_TARBALL_SHA256` to the new release's checksum. The tarball URL is derived from
  the version; the JRE is resolved + checksum-verified against the Adoptium API at fetch
  time (nothing to pin).
- **XMage dist** (`pipeline/sim/xmage_runtime.py` + `.github/workflows/xmage-dist-release.yml`):
  1. If the upstream XMage version changes, update `XMAGE_VERSION` (runtime), the dist
     pom's `<xmage.version>`, and the workflow's `XMAGE_TAG` (the upstream build ref) —
     all coherent.
  2. Whenever the shaded jar's **contents** change (new module, harness edit) — even
     with no upstream bump — bump `_DIST_TAG` (tags are immutable; e.g. `…-2`), and
     re-arm `XMAGE_DIST_SHA256 = None`.
  3. Push the `xmage-dist-*` git tag → the release workflow reactor-builds, shades,
     **license-audits**, smoke-tests, and uploads the jar + `.sha256` + `THIRD-PARTY.txt`.
  4. Pin `XMAGE_DIST_SHA256` **from the release's `.sha256` asset** — never a local
     rebuild (shaded jars aren't byte-reproducible) — and commit.
  5. Verify: a fresh empty `MAKE_MAGIC_DATA_DIR` (no `MAKE_MAGIC_XMAGE_HOME`) →
     `simulate deck … --engine xmage` fetches, SHA-verifies, and runs.

Until the SHA is pinned, `ensure()` fails **closed** (refuses to fetch) — the CI guard
also fails a merge left in that state.

## Reporting issues

Use the [issue tracker](https://github.com/trippersham/magic-tools/issues). For a
simulation bug, include the `scripts/simulate doctor` output and, where relevant,
the stored game log (`scripts/simulate log …`).

This project is unofficial Fan Content and is not affiliated with Wizards of the
Coast.
