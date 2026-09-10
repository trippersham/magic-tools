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

### Added

- **`collection deck-combos` returns the full combo line, not just a label.** Each detected
  combo now carries the ORDERED assembly `steps` (the Spellbook `description`, newline-split
  and stripped), the non-empty `prerequisites` (easy → notable → mana needed, in source
  order), a typed `produces` list (`{name, status, win}` with a per-feature game-win litmus),
  and a `spellbookUrl`, alongside the existing `variant_id` / `cards` / `result`. Purely
  **additive / non-breaking**: a pure projection widening — combo detection and the
  game-win litmus are untouched, the widened fields default to empty and round-trip through
  a new `steps` / `prerequisites` / `produces` column set in the normalized lake, and every
  legacy `Combo(...)` call site (seeds, driver-batch reconstruction) stays valid.

## [0.7.2] — 2026-09-11

A **non-breaking** simulation-integrity release (a *patch* under this project's `0.y.z`
convention). Closes a lethality hole in the sim that credited fabricated combo wins;
everything existing is unchanged — no CLI removals, no store or Airtable schema changes,
and no XMage-dist re-pin (the fix is Python plus a local harness-jar recompile against
the existing `-3` dist).

### Fixed

- **Lethality invariant — drivers can no longer cheat their wincons.** A driven combo
  driver's macro-fold could fire on a mere `applicable()` precondition and be credited a
  win with the opponent still alive and the combo unassembled. Across the durable 16k-game
  corpus this fabricated **887 wins (6.8% of decided games, 17.5% of driven games)** and
  affected **91% of driven decks** — e.g. a Paul/Queza build's reported 58.3% was an honest
  32.0%. A win is now real only if the loser reached a **substantive terminal** (life ≤ 0 /
  deckout / poison / commander damage):
  - the goldfish `killed` signal requires a real lethal (`lifeB ≤ 0`), dropping the
    `|| hasLost()` hole (committed harness jar rebuilt against the `-3` dist, `javac
    --release 17`);
  - the driver gate **fails** a proactive quad that fires its macro but never reaches a
    real lethal, even when the baseline also never kills;
  - the seed `applicable()` no longer scans graveyard/exile (an uncastable piece can't
    complete the combo, so it must not keep the macro firing).

### Added

- **Win-lethality audit** (`python -m pipeline.sim.win_audit`) — honest per-deck win-rates
  that exclude fabricated macro wins, plus `transcript_parser` helpers (`loser_life()`,
  `win_lethality()`, `is_fake_macro_win()`) that make existing corpora honest without a
  re-run.

## [0.7.1] — 2026-09-08

A **non-breaking** release (a *patch* under this project's `0.y.z` convention). Adds an
external-deck ingestor and a full-card-image lake column; everything existing is
unchanged and no store or Airtable schema changes.

### Added

- **Full card image in the lake** (`image_normal`) — the Scryfall oracle-card projection
  now carries `image_uris.normal` (the full framed card, alongside the existing
  `art_crop`), threaded through the resolver's select/live/lake paths and the `Card`
  contract. `get-card` serves the full image **lake-backed**, with a live fetch only on a
  true miss — so bulk image resolution is deterministic and network-free for cards already
  in the lake. Additive / non-breaking (a new nullable column; DFC/split faces are null,
  mirroring `art_crop`).
- **`app/` deck-diff viewer** — a static Astro app under the repo-root `app/` (dev tooling,
  outside the shipped `make-magic` plugin boundary) that renders a deck alongside a proposed
  changeset — six diff views, quantity-aware/per-instance curation, faceted grouping
  (type / color / labels). Not part of the plugin package; consumes make-magic deck data
  and the new `image_normal` lake column.
- **Deck ingestor layer** — `collection import-deck <url|file|->` imports an external
  deck into an ephemeral draft, then every downstream verb (`get-deck`, `factsheet`,
  `deck-swap`, …) works on it. Supported sources: **Archidekt** and **EDHREC** URLs
  (public APIs, no creds), **Moxfield** URLs (best-effort — the Cloudflare WAF blocks
  automated reads, so a block degrades to an actionable "Export → paste the list, pipe
  it to `import-deck -`" error rather than a traceback), and **plaintext / Forge `.dck`**
  lists (a file, `-`/stdin, or a paste, with `*CMDR*` / `Commander:` commander markers).
  Imports cache a normalized `RawDeck` under `data/raw/deck_import/` on a short TTL
  (paste-sourced entries are permanent); `--refresh` forces a re-fetch. Purely
  **additive / non-breaking**: a new verb + a new read-side source sub-package, no
  changes to existing decks, verbs, or the Airtable schema.

## [0.6.2] — 2026-08-23

A **non-breaking** simulation release (a *patch* under this project's `0.y.z`
convention — no minor bump). XMage joins Forge as a co-equal, opt-in sim engine;
everything existing is unchanged (`--engine` defaults to `forge`, all new behaviour is
opt-in). Upgrading is safe: no CLI removals, no store or Airtable schema changes.

### Added

- **XMage as a co-equal sim engine** (`--engine xmage`) — a headless
  ComputerPlayer7 (minimax) that actively casts counters and sequences interaction,
  the control-piloting strength Forge's AI lacks. Emits the same `Game Result:` line
  contract, so the whole telemetry/parser pipeline consumes it unchanged.
- **`--engine both`** — a side-by-side comparison: per-engine win-rate ± Wilson CI,
  records, and interaction piloting (the counter-specific "false read" check + Δ).
- **XMage 1v1 Commander (EDH)** — `--format commander --engine xmage` runs a real
  `CommanderDuel` (40 life); the commander loads via the deck sideboard into the
  command zone. Curated EDH gauntlet decks now use castable, on-color commanders.
- **Runtime auto-provisioning for XMage** — a self-contained shaded distributable jar
  fetched from GitHub Releases (SHA256-pinned, fail-closed, re-verified on cache hit),
  so a fresh box needs no local reactor build — mirroring Forge's fetch-at-runtime.

### Changed

- The concurrency governor is hardened for the heavier XMage JVMs: per-engine RAM
  budgets, an admission floor that tracks them, a continuous resource sampler that can
  abort (and terminate in-flight JVMs) under sustained pressure, and per-run private H2
  card-DB copies (COW where available) that remove a parallel-init race.

### Fixed

- **Fresh Forge install was broken:** the Adoptium JRE metadata URL used
  `/v3/assets/latest/21/ga` (a 404) instead of `/21/hotspot`, so every clean Forge
  provision failed at the JRE step. The JRE is now resolved before the large Forge
  tarball is fetched (fail-fast).
- The "unusable run" exit-code guard divided failures by successes only, tripping
  non-zero at ~⅓ failures instead of the documented majority; it now uses the total.
- XMage jar fetch is atomic (stage + `os.replace`) and a non-timeout JVM error no
  longer orphans the process group.

### Infrastructure

- **Release process formalized and CI-enforced** (see CONTRIBUTING → *Cutting a
  release*): lockstep plugin/package version guard, XMage dist SHA pin ↔ published
  release, harness-jar bytecode reproducibility (both engines), and a weekly
  upstream-dep canary (Adoptium JRE + Forge tarball still resolve — the exact break
  that shipped the `/21/ga` 404).


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
