# magic-tools

A Claude Code plugin marketplace for Magic: The Gathering.

## make-magic

Deck building, card chasing, inventory management, and **game simulation** —
powered by **Scryfall**, with an **optional** shared Airtable base. Skills:
**building-decks**, **chasing-cards**, **managing-inventory**,
**simulating-games**.

**Works out of the box with no account or credential** — card data from Scryfall,
your collection in a local store by default. Airtable is opt-in.

**simulating-games** plays real, rules-enforced AI-vs-AI games via
[MTG Forge](https://github.com/Card-Forge/forge) and reports a win-rate ± CI plus
a numerical telemetry profile — an empirical read on *how a deck actually plays*.
Forge and a Java runtime are **fetched at runtime on first use** (a one-time
~350 MB download, checksum-verified and cached); nothing is bundled or
redistributed. See the [simulate section](plugins/make-magic/README.md#simulating-games)
and [NOTICE](NOTICE).

```bash
claude plugin marketplace add trippersham/magic-tools
claude plugin install make-magic@magic-tools
```

Then `/reload-plugins` (or restart). Verify with `${CLAUDE_PLUGIN_ROOT}/scripts/collection status`
(expect `"backend": "local"`) — no credentials needed.

→ **Full quickstart, local-mode usage, and optional Airtable setup:
[plugins/make-magic/README.md](plugins/make-magic/README.md).**

Supported OS: macOS / Linux (Forge-backed simulation is macOS/Linux only; on
Windows use WSL2). `uv` self-provisions; Node 18+ is only needed for the
optional Airtable MCP.

## License

make-magic — Copyright (C) 2026 Tripp Wickersham.

This program is free software: you can redistribute it and/or modify it under the
terms of the **GNU General Public License v3.0** (or, at your option, any later
version), as published by the Free Software Foundation. It is distributed WITHOUT
ANY WARRANTY. See [LICENSE](LICENSE) for the full text.

The game-simulation feature drives [MTG Forge](https://github.com/Card-Forge/forge)
(GPL) and a Temurin JRE (GPL v2 + Classpath Exception) — both **fetched at runtime
on the user's machine, arms-length, and never bundled or redistributed** by this
project. See [NOTICE](NOTICE) for the third-party attribution and how the
arms-length subprocess relationship keeps make-magic's own license unentangled.

Unofficial Fan Content — not affiliated with or endorsed by Wizards of the Coast.
See [CONTRIBUTING.md](CONTRIBUTING.md) to contribute and [CHANGELOG.md](CHANGELOG.md)
for release notes.
