# magic-tools

A Claude Code plugin marketplace for Magic: The Gathering.

## make-magic

Deck building, card chasing, and inventory management — powered by **Scryfall**,
with an **optional** shared Airtable base. Skills: **building-decks**,
**chasing-cards**, **managing-inventory**.

**Works out of the box with no account or credential** — card data from Scryfall,
your collection in a local store by default. Airtable is opt-in.

```bash
claude plugin marketplace add trippersham/magic-tools
claude plugin install make-magic@magic-tools
```

Then `/reload-plugins` (or restart). Verify with `${CLAUDE_PLUGIN_ROOT}/scripts/collection status`
(expect `"backend": "local"`) — no credentials needed.

→ **Full quickstart, local-mode usage, and optional Airtable setup:
[plugins/make-magic/README.md](plugins/make-magic/README.md).**

Supported OS: macOS / Linux. `uv` self-provisions; Node 18+ is only needed for the
optional Airtable MCP.
