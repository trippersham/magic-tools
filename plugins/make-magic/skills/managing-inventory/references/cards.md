# Managing Cards (via the `collection` CLI)

All card operations go through the backend-agnostic CLI — identical in local YAML and Airtable
mode. The resolver hydrates Scryfall metadata (type, CMC, oracle text, art, price, color
identity, keywords) from the card **name**, so you pass a name plus ownership details, not a
field map. There is no `mcp__airtable__*` write step.

## Add Individual Cards

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection add-card "<Card Name>" \
  --qty <n> --condition NM --set <SET> --source "<provenance>"
```

- `--qty` — copies to add (default 1)
- `--condition` — condition grade, repeatable (e.g. `--condition NM --condition LP`)
- `--foil` — number of foil copies
- `--set` — printing owned (set code / name), repeatable
- `--source` — acquisition provenance, repeatable

To correct an existing count: `set-quantity "<Card Name>" <n>`.
To drop a card entirely: `remove-card "<Card Name>"`.

## Reading inventory

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection list-inventory
```
Each `OwnedCard` carries `name`, `owned`, `foil`, `condition`, `sets`, `sources`, plus the
resolved `type_line` / `mana_value` / `oracle_text` / `color_identity` / `keywords`.

## Metadata / price notes

- **Local mode** hydrates metadata on the fly from the Scryfall cache — there is no persisted
  metadata column to backfill. `add-card` / `set-quantity` are single-card writes.
- **Airtable mode** stores metadata columns on the base. A bulk *refresh* of those columns (the
  old "backfill" dance — page rows, re-hydrate from Scryfall, batch-update) is an Airtable-only
  maintenance job that reads via `/mcp` and re-hydrates; see the Optional / ad-hoc appendix in
  SKILL.md. It is not a skill write path — per-card corrections still go through the CLI.
- **Null price:** if `prices.usd` is null for a printing, the resolver / `scryfall_batch.py`
  tries alternate printings via `/cards/search?q=!"CARD NAME"&unique=prints` and takes the
  cheapest non-null USD.
