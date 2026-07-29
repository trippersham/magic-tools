# make-magic

MTG deck building, card chasing, and inventory management for Claude Code —
powered by **Scryfall**, with an **optional** shared Airtable base.

Skills: **building-decks**, **chasing-cards**, **managing-inventory**.

> **Works out of the box — no account, no credential.** Card data comes from
> Scryfall; your collection lives in a local store by default. Point it at a
> shared Airtable base only if you want to (see [Airtable (optional)](#airtable-optional)).

Supported OS: **macOS / Linux**. You do **not** need to install `uv` — the plugin
self-provisions a pinned copy on session start. Node 18+ is only needed for the
optional Airtable MCP.

---

## Install

```bash
claude plugin marketplace add trippersham/magic-tools
claude plugin install make-magic@magic-tools
```

Then `/reload-plugins` (or restart Claude Code).

- **Desktop:** Settings → Plugins → Add marketplace `trippersham/magic-tools` → install `make-magic`.
- **Web (Cowork):** this repo's `.claude/settings.json` enables the plugin automatically — nothing to install.

---

## Verify — no credentials needed

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/collection status
```

Expect `"backend": "local"`. That single command proves `uv` self-provisioned, the
pipeline loaded, and you're ready — with zero setup.

---

## Quickstart (local mode)

Just talk to the skills — *"build a Krenko goblins deck"*, *"what should I chase
for my Krenko deck?"*, *"add Sol Ring to my inventory"* — or drive the CLI directly:

```bash
C="${CLAUDE_PLUGIN_ROOT}/scripts/collection"

"$C" onboard --backend local              # pin local as your source of record (optional — local is the default)
"$C" add-card "Sol Ring"                   # add an owned card (hydrated live from Scryfall)
"$C" add-card "Krenko, Mob Boss" --qty 1 --foil 1   # --foil is a COUNT, not true/false
"$C" add-chase "Ragavan, Nimble Pilferer"  # track a card you want to acquire
"$C" save-deck --from-json - <<'JSON'
{
  "name": "Krenko Goblins",
  "cards": [
    {"name": "Krenko, Mob Boss", "role": "commander"},
    {"name": "Goblin Chieftain"},
    {"name": "Lightning Bolt", "quantity": 1}
  ]
}
JSON
"$C" get-deck "Krenko Goblins"             # full deck JSON incl. cards[]
"$C" factsheet "Krenko Goblins"            # neutral curve / ramp / interaction / otag analysis
```

The **commander** is a `cards[]` entry tagged `"role": "commander"` — not a
top-level field. Run any verb with `-h` for its flags (e.g. `collection add-card -h`).

**Where local data lives:** under `MAKE_MAGIC_DATA_DIR` (default: the plugin's
`pipeline/data/`). Set that env var to relocate the whole store — lake, DuckDB, and
`collection/` YAML — somewhere else (tests and isolated setups use it).

Card resolution is offline-first from a local DuckDB lake with a live Scryfall
fallback, so day-one usage works immediately; you never have to bulk-download
anything to get started.

---

## Airtable (optional)

Prefer a shared, multi-device Airtable base as your source of record instead of
local YAML? Opt in:

1. **Choose the backend** — `collection onboard --backend airtable`, or set
   `MAKE_MAGIC_BACKEND=airtable`. (Resolution order: explicit `MAKE_MAGIC_BACKEND`
   → your onboarded choice → `AIRTABLE_API_KEY` present → else `local`.)
2. **Provide the credential** — `AIRTABLE_API_KEY`, an Airtable **Personal Access
   Token** with read access to the shared "Magic Inventory" base.

**Scope the token narrowly.** The bundled Airtable MCP exposes write tools, so a
read-mostly workflow is best served by a **read-scoped, minimal PAT** — grant only
`data.records:read` / `schema.bases:read`, limited to the "Magic Inventory" base.

**Where to set it** (resolution order, see `.mcp.json`):
1. An ambient `AIRTABLE_API_KEY` in the environment always wins.
2. Otherwise a gitignored `plugins/make-magic/.env` is sourced:
   ```bash
   # plugins/make-magic/.env   (gitignored — copy from .env.example)
   AIRTABLE_API_KEY=your_token_here
   ```
   On **Cowork**, set it in the environment's **Environment Variables** field instead.

<details>
<summary>Optional: resolve the token from 1Password (local, macOS/Linux)</summary>

Instead of a raw token in `.env`, resolve it at load time (placeholders only —
substitute your own account/vault/item; the gitignored `.env` keeps the `op://`
path out of the repo):

```bash
export AIRTABLE_API_KEY="$(op read --account <your-1p-account> 'op://<Vault>/<Item>/credential')"
```
</details>

---

## How it works

- **Card data** — Scryfall, offline-first via a local DuckDB "medallion" lake
  (`raw → normalized → marts`), with a paced live fallback for cache misses.
- **Collection** — local `collection/` YAML by default, or Airtable records when
  configured. The skills read/write through one backend-agnostic `collection` CLI.
- **Analysis** — a neutral fact sheet (curve, pips, ramp, interaction, otag
  buckets) plus otag-informed strategy fit; the skills own the judgment calls.

Env vars: `MAKE_MAGIC_BACKEND` (`local` | `airtable`), `MAKE_MAGIC_DATA_DIR`
(local store location), `AIRTABLE_API_KEY` (Airtable mode only).
