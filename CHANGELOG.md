# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/).

> **Versioning:** the plugin (`.claude-plugin/plugin.json`) and the bundled
> `make-magic-pipeline` Python package (`plugins/make-magic/pipeline/pyproject.toml`)
> share a single **lockstep** version. While the project is pre-1.0, a bump of the
> **minor** field (`0.x`) is treated as the major/breaking increment per the semver
> `0.y.z` convention — an upgrade across it may include breaking changes.

## [Unreleased]

## [0.6.1] — 2026-08-05

A **non-breaking** deckbuilding rework (a *patch* under this project's `0.y.z`
convention — no minor bump, no breaking changes). A deck being built is now a real,
typed deck in a local store — the same model, guards, and ceremony as any persisted
deck — instead of a bespoke parallel draft structure. Upgrading is safe: on first run
each local `collection/decks/*.yaml` gains an additive, stable `uuid` field (atomic,
non-destructive write); no cards, quantities, strategies, or Airtable records change,
and there is no Airtable schema change.

### Added

- **Local decks store (DuckDB working-copy layer)** fronting the source of record.
  **Ephemeral drafts** (local-only experiments) vs **synced decks** (backed by YAML or
  Airtable): explore in a `new-draft --from` copy without touching the original, then
  `promote-deck` to commit (the copy auto-retires).
- **Deckbuilding skills** — `building-decks` reworked as a derived-phase orchestrator
  (FRAME → ASSESS → REFINE → VALIDATE → COMMIT, staleness derived from provenance, one
  hard gate at commit) delegating to three new skills: **`distilling-strategy`**,
  **`assessing-decks`**, **`refining-decks`**.
- **New `collection` verbs** — `new-draft`, `promote-deck`, `deck-swap`, `deck-add`,
  `deck-remove`, `undo-deck` (rationale-logged), `deck-combos` (archetype-fidelity
  signal), `stamp-sim`, `pull` / `push` / `sync`, `archive-deck` / `unarchive-deck`,
  and `get-deck --provenance` / `--local` / `--id`.
- **Provenance stamps** — assessment + sim freshness (`fresh` / `stale` / `absent`),
  surfaced on `get-deck --provenance` and `list-decks --json`, so a build resumes
  correctly across sessions.
- **Stable-identity binding** — decks bind to an in-file `uuid` (YAML) or the record id
  (Airtable), so renames and duplicate names never mis-target the source of record.

### Changed

- Deck reads/edits route through the local decks store (reads served from a cached
  copy, pulled current on a short TTL; edits commit through to the source).
- `list-decks` output carries status markers (`[synced]` / `[ephemeral]` /
  `[synced,source-missing]`) — adjust any script that parsed its output.
- `factsheet` now works on ephemeral drafts (routes through the store).
- Deck edits are **guard-enforced**: operations that previously slipped through
  silently (e.g. `deck-remove --qty -1`, cutting the sole commander, writing to a
  deleted source) are now **refused with a clear message** — stricter, not lossy.

### Fixed

- The deck-drift / silent-clobber class (data loss to a shared source of record) is now
  **impossible by construction** — the store enforces every deck invariant, source
  reads bind by stable identity (never by name), and a dead/gone source is a refusal for
  writes, with safe-by-construction recovery via a fresh-identity `save-deck`.

## [0.6.0] — 2026-08-02

First tagged public release — a **major (pre-1.0) drop**. It adds fetch-at-runtime
Forge/JRE provisioning, relicenses the project **GPL-3.0-or-later**, and establishes
the `simulate` CLI + deck schema surface. Anyone pinned to a `0.5.x`-era build should
re-review on upgrade.

### Added

- **`simulating-games` skill + `scripts/simulate` CLI** — Forge-backed AI-vs-AI game
  simulation. Evaluate a deck against a gauntlet (`simulate deck`), A/B two variants
  (`simulate ab`), or play a single head-to-head (`simulate match`); results report
  win-rate ± 95% CI plus a numerical telemetry profile (kill-turn, win-margin,
  wincon mix, ramp curve).
- **Fetch-at-runtime Forge + JRE provisioning** — on first use, make-magic downloads
  a pinned MTG Forge release (SHA256-verified) and, if needed, an Eclipse Temurin
  JRE from Adoptium, caching both under the data dir. Nothing is bundled or
  redistributed. `simulate doctor` reports availability + the safe JVM pool size;
  `simulate doctor --provision` fetches on demand.
- **`guilds` gauntlet bundle** — a shipped 30-deck field: 10 two-color guilds ×
  weak/mid/strong power tiers (`--gauntlet guilds`). Plus a small default `curated`
  field and `mine`/`both` (your own decks).
- **Per-game log retention + `simulate log`** — every simulated game's verbose Forge
  log is stored in DuckDB and retrievable for forensic deep-diving (Forge's RNG seed
  is not reproducible, so logs are captured, never re-derived).
- **Deck-name sanitization + Forge-loadable-name validation** — decks export to
  Forge `.dck` with filesystem-safe names; MDFC combined names (`A // B`) are
  rewritten to the front face Forge's loader accepts, and cards absent from Forge's
  DB are caught before a run (`--allow-missing` to override).
- **Content-addressed matchup cache** in DuckDB — an unchanged deck-vs-deck matchup
  is never re-simulated; `--force` bypasses.
- **Deck sideboard/role support** — decks carry maindeck, commander, and sideboard
  cards; sideboard cards render into the `.dck` `[Sideboard]` section and round-trip
  through both the local-YAML and Airtable backends.
- **License, NOTICE, CONTRIBUTING, CHANGELOG** — the project is now licensed
  GPL-3.0-or-later, with third-party (Forge / Temurin) attribution.

### Changed

- Forge deck staging is now **per-platform** (macOS `~/Library/Application Support/Forge`,
  Linux `~/.forge`), so the `simulate` feature works on Linux, not just macOS.
- The first-run ~350 MB Forge/JRE download is **consent-gated** — it prompts on an
  interactive terminal (or pass `--yes`), and auto-proceeds when non-interactive so
  agents/CI aren't blocked.
- **Windows is explicitly unsupported** — a clean early error (use WSL2) instead of a
  cryptic mid-run JVM failure.

### Security

- **First-run JRE integrity is fail-closed.** A missing or unfetchable Temurin
  checksum now aborts provisioning instead of silently skipping verification, and all
  downloads reject non-HTTPS redirect targets — closing a gap where an unverified JRE
  could be fetched and executed on a hostile network. (The Forge tarball was already
  SHA256-pinned and verified before extraction.)

### Fixed

- Governor results are paired to their exact matchup spec (not by deck name),
  preventing cache misattribution across duplicate deck names.
- The external JVM timeout reaps the whole process group, so a hung Forge run under
  `xvfb-run` cannot leak a grandchild JVM.
- Sideboard basics and quantities no longer corrupt the maindeck on save; the
  deck-shrink safety guard counts the maindeck only.
- Sideboard cards on the `.dck` path are now covered by the Forge card-availability
  guard (previously only `[main]`/`[commander]` were validated).
- Cleared all strict-`pyright` errors and aligned the `add_chase` port/adapter return
  contract across the collection backends.

### Infrastructure

- **Offline CI** (`pytest` + `ruff check` + `ruff format --check` + `pyright`) runs on
  every push and pull request.
