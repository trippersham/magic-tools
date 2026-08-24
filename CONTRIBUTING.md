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

Three pytest markers are deselected by default (they need external resources):

- `-m live` — exercises a real Airtable base (needs `AIRTABLE_API_KEY`).
- `-m forge` — runs **real** headless MTG Forge games. Needs a Forge install; set
  `MAKE_MAGIC_FORGE_HOME` + `MAKE_MAGIC_JAVA`, or let `scripts/simulate doctor
  --provision` fetch Forge + a JRE first. These spawn JVMs — the concurrency
  governor caps the pool, but run them deliberately.
- `-m canary` — network checks that the runtime-fetched upstream deps (the Adoptium
  JRE, the Forge tarball) still resolve. Run weekly by `.github/workflows/upstream-canary.yml`;
  a red canary means an upstream dep moved (not a code regression).

```bash
uv run --extra dev pytest -m forge     # only when you have Forge available
```

### Harness jars

The committed harness jars (`pipeline/sim/java/forge-simai/make-magic-forge-simai.jar`,
`pipeline/sim/java/xmage/make-magic-xmage.jar`) are **our** compiled code. If you edit a
harness source under `pipeline/sim/java/*/src`, rebuild + commit its jar (`build.sh` in
that dir). CI verifies the committed jar's **bytecode** matches a fresh build from source
(`forge-simai-build.yml`; the `verify-committed-harness-jar` job in `xmage-dist-release.yml`),
so a stale jar is a red build.

## Conventions

- **Style:** ruff (single quotes; see `pyproject.toml`). Keep the codebase's
  high docstring density — especially the *why* behind empirically-derived Forge
  constants.
- **Commits:** conventional-commit style (`feat(sim): …`, `fix(collection): …`).
- **Scope:** the pipeline is *additive* to the collection/deck-building workflow and
  must never degrade the local-first, no-credential default path.

## Cutting a release

The plugin and the bundled `make-magic-pipeline` package share **one lockstep version**.
Per this project's `0.y.z` convention a **minor** bump (`0.x`) signals *breaking* changes,
so a non-breaking release is a **patch**.

### 1. Ship the plugin version

1. **CHANGELOG** — move `CHANGELOG.md`'s `[Unreleased]` items into a new
   `## [X.Y.Z] — <date>` section ([Keep a Changelog](https://keepachangelog.com/)).
2. **Bump the version in _both_ files, to the same value:**
   - `plugins/make-magic/.claude-plugin/plugin.json` → `version`
   - `plugins/make-magic/pipeline/pyproject.toml` → `project.version`

   (`.claude-plugin/marketplace.json` carries no version — it references the plugin by
   path.)
3. **Tag + Release** — tag `vX.Y.Z` and publish a matching GitHub Release (title from the
   changelog entry).

### 2. Bump a vendored install (only when changing Forge / XMage / the JRE)

These are fetched at runtime and pinned by SHA:

- **Forge** (`pipeline/sim/forge_runtime.py`): bump `FORGE_VERSION` **and** re-pin
  `FORGE_TARBALL_SHA256`. The URL derives from the version; the JRE is resolved +
  checksum-verified against Adoptium at fetch (nothing to pin).
- **XMage dist** (`pipeline/sim/xmage_runtime.py` + `xmage-dist-release.yml`): on an
  upstream bump keep `XMAGE_VERSION`, the pom's `<xmage.version>`, and the workflow's
  `XMAGE_TAG` coherent. **Whenever the shaded jar's contents change** (new module,
  harness edit) — even with no upstream bump — bump `_DIST_TAG` (tags are immutable,
  e.g. `…-2`) and re-arm `XMAGE_DIST_SHA256 = None`. Push the `xmage-dist-*` tag → the
  workflow builds → **license-audits** → smoke-tests → uploads the jar + `.sha256`. Pin
  `XMAGE_DIST_SHA256` **from the release's `.sha256` asset** (never a local rebuild —
  shaded jars aren't byte-reproducible), commit, then verify onboarding on a fresh empty
  `MAKE_MAGIC_DATA_DIR`.
- **Harness jars**: if you edit a source under `pipeline/sim/java/*/src`, rebuild +
  commit its jar (`build.sh` in that dir).

### Enforced in CI (drift = red build)

- lockstep plugin/package version agreement (`tests/test_versioning.py`);
- `XMAGE_DIST_SHA256` is pinned **and** matches the published release
  (`verify-published-pin`) — `ensure()` fails **closed** until it is;
- harness-jar bytecode reproduces from source, both engines (`forge-simai-build.yml`,
  `verify-committed-harness-jar`);
- a weekly canary that the JRE + Forge tarball still resolve upstream
  (`upstream-canary.yml`).

## Reporting issues

Use the [issue tracker](https://github.com/trippersham/magic-tools/issues). For a
simulation bug, include the `scripts/simulate doctor` output and, where relevant,
the stored game log (`scripts/simulate log …`).

This project is unofficial Fan Content and is not affiliated with Wizards of the
Coast.
