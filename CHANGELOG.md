# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/).

> **Versioning:** the version tracked here is the **plugin** version
> (`.claude-plugin/plugin.json`) — the user-facing artifact installed from the
> marketplace. The bundled `make-magic-pipeline` Python package
> (`plugins/make-magic/pipeline/pyproject.toml`) carries its own independent
> `0.x` version and is an internal implementation detail, not published to PyPI.

## [Unreleased]

## [0.5.0] — 2026-08-02

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
- **License, NOTICE, CONTRIBUTING, CHANGELOG** — the project is now licensed
  GPL-3.0-or-later, with third-party (Forge / Temurin) attribution.

### Fixed

- Governor results are paired to their exact matchup spec (not by deck name),
  preventing cache misattribution across duplicate deck names.
- The external JVM timeout reaps the whole process group, so a hung Forge run under
  `xvfb-run` cannot leak a grandchild JVM.
