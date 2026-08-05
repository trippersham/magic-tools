# make-magic

MTG **deck building**, **card chasing**, **inventory management**, and **AI-vs-AI game
simulation** for Claude Code — powered by **Scryfall**, with an **optional** shared
Airtable base.

> **Works out of the box — no account, no credential.** Card data comes from
> Scryfall; your collection lives in a local store by default. Point it at a
> shared Airtable base only if you want to (see [Airtable (optional)](#airtable-optional)).

Supported OS: **macOS / Linux** (the Forge-backed `simulate` verbs are
macOS/Linux only; on **Windows use WSL2**). You do **not** need to install `uv`
— the plugin self-provisions a pinned copy on session start. Node 18+ is only
needed for the optional Airtable MCP.

---

## Capabilities

Everything is driven by talking to the skills in natural language; each is also
backed by a scriptable CLI (`collection` / `simulate`).

| Skill | What it does |
|---|---|
| **building-decks** | The deckbuilding orchestrator — guides a deck from an idea (or an existing list) to a committed, validated deck, delegating each stage below. |
| **distilling-strategy** | Author a deck's **Strategy** (the game plan + what a card must do to earn a slot). |
| **assessing-decks** | Diagnose a deck from a **neutral fact sheet** (curve, ramp, interaction, Quadrant-Theory balance) and write its **Assessment**. |
| **refining-decks** | Propose **ranked, size-preserving swaps** grounded in the Strategy, and apply the accepted ones. |
| **simulating-games** | Play **real, rules-enforced AI-vs-AI games** via MTG Forge and report **win-rate ± CI** plus a telemetry profile. |
| **chasing-cards** | Track and prioritize cards you want to acquire. |
| **managing-inventory** | Add/update owned cards (live-hydrated from Scryfall) and vet trades. |

Two backend-agnostic CLIs sit under the skills: **`collection`** (decks, inventory,
chase, trades) and **`simulate`** (Forge games). Every deck read/edit/lifecycle
step goes through `collection` and behaves identically on local YAML or Airtable.

---

## Install

```bash
claude plugin marketplace add trippersham/magic-tools
claude plugin install make-magic@magic-tools
```

Then `/reload-plugins` (or restart Claude Code).

- **Desktop:** Settings → Plugins → Add marketplace `trippersham/magic-tools` → install `make-magic`.
- **Web (Cowork):** this repo's `.claude/settings.json` enables the plugin automatically — nothing to install.

### Verify — no credentials needed

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

## Building decks

`building-decks` runs a guided, **derived** build: each turn it reads the deck,
figures out the earliest thing that's missing or stale, and routes to the skill
that fixes it — **FRAME** (strategy) → **ASSESS** (fact sheet + assessment) →
**REFINE** (ranked swaps) → **VALIDATE** (simulate + archetype-fidelity) →
**COMMIT**. There is no state machine to get out of sync: staleness is *derived*
from provenance stamps on the deck, so a build picked up in a later session
resumes correctly.

**A deck being built is a real, typed deck** — the same model, guards, and
ceremony as any persisted deck, held in a local store. Two kinds:

- **Synced** — backed by your source of record (local YAML or Airtable). Reads
  pull it current; edits commit through.
- **Ephemeral** — a local-only draft. Explore freely without touching anything on
  the source; commit when you're happy, or archive it if you're not.

```bash
C="${CLAUDE_PLUGIN_ROOT}/scripts/collection"

# Improve an existing deck safely: branch a local exploration copy, edit it,
# then commit the result back onto the original (the copy is auto-retired).
"$C" new-draft "Krenko (explore)" --from "Krenko Goblins"
"$C" deck-swap "Krenko (explore)" --add "Goblin Recruiter" --cut "Lightning Bolt" --why "tutor density"
"$C" promote-deck "Krenko (explore)" --to "Krenko Goblins"

# Build clean-slate: an ephemeral draft, grown locally, promoted at the end.
"$C" new-draft "New Brew" --commander "Krenko, Mob Boss" --format Commander
"$C" set-strategy "New Brew" "Go wide on goblins, then alpha strike."
"$C" deck-add "New Brew" "Goblin Chieftain"
"$C" promote-deck "New Brew" --to "New Brew"

"$C" undo-deck "Krenko Goblins"                 # step back one edit (rationale-logged)
"$C" get-deck "Krenko Goblins" --provenance     # assessment/sim freshness: fresh | stale | absent
"$C" deck-combos "Krenko Goblins"               # named-card combos present (archetype-fidelity signal)
"$C" list-decks                                 # decks + status: [synced] / [ephemeral]
```

**The store enforces the invariants** — you cannot construct an illegal deck: a
single copy per non-basic card, exactly one commander (no cutting the sole
commander, no commander at quantity 2), quantity ≥ 1, a size/shrink guard, and
size-preserving, commander-safe swaps. A bad edit is **refused with a clear
message**, not silently applied. Decks are addressed by **name**; if a name is
ambiguous the CLI lists the candidates and you re-run with `--id <prefix>`.

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

Deck identity binds to the Airtable **record id**, so renaming a deck (on either
side) never loses the link, and make-magic never creates or deletes a Decks record
except through an explicit, guarded save.

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

## Simulating games

`simulating-games` plays **real, rules-enforced AI-vs-AI games** via
[MTG Forge](https://github.com/Card-Forge/forge) and reports **win-rate ± CI**
plus a numerical **telemetry profile** (kill-turn, win-margin, wincon mix, ramp
curve). It answers one question empirically: *how does this deck actually play?*

**No manual setup — Forge and Java self-provision.** The first time you run a
game verb, make-magic downloads a pinned MTG Forge release and (if you don't
already have a suitable Java) an Eclipse Temurin JRE — a **one-time ~350 MB
download**, checksum-verified and cached under your data dir, reused thereafter.
Nothing is bundled or redistributed; the download happens on your machine from
the official upstreams (see the repo [NOTICE](../../NOTICE)). Already have Forge
and/or Java? Point at them with `MAKE_MAGIC_FORGE_HOME` / `MAKE_MAGIC_JAVA` and no
download occurs. **`uv` and Java both self-provision — you do not need to install
a JDK.**

On a **first** game verb in an interactive terminal, the ~350 MB download is
gated on a `[y/N]` confirmation (so it never surprises you on a metered
connection); pass `--yes`/`-y` to skip the prompt, or run `simulate doctor
--provision` to fetch explicitly ahead of time. Non-interactive runs (agent / CI)
auto-proceed. The JRE is downloaded only over HTTPS (redirect downgrades are
refused) and SHA256-verified before its `java` is ever executed.

Check the environment first (offline, no download, no game):

```bash
S="${CLAUDE_PLUGIN_ROOT}/scripts/simulate"

"$S" doctor                    # reports Forge/Java availability + the safe JVM pool size
"$S" doctor --provision        # fetch Forge + JRE now (the one-time ~350 MB download)
```

Then evaluate a deck against a bundled gauntlet of opponents:

```bash
"$S" deck "Krenko Goblins" --gauntlet guilds --games 30
# a .dck file works too:  "$S" deck path/to/deck.dck --gauntlet curated --games 30
"$S" ab "Krenko Goblins" variant.dck --gauntlet curated --games 30   # A/B two variants
"$S" gauntlet show --source guilds                                    # list a bundled field
```

- **Gauntlets** — `curated` (a small default field), `guilds` (the shipped 30-deck
  bundle: 10 two-color guilds × weak/mid/strong power tiers), or `mine`/`both`
  (your own decks, needs a collection backend). Bundles are format-specific
  (`guilds` is constructed).
- **Sample size** — `--games` is *per opponent*. A few games is a smoke; budget
  **~300 games per finalist** for a tight verdict (see the skill's guardrails).

> **Read Forge results as directional, not ground truth.** Forge's opponents are a
> rule-based AI: competent at fair beatdown/midrange, weak at control/combo/stax.
> A deck that *can't* beat the gauntlet is genuinely flawed; a deck that *beats* it
> is confirmed functional, not confirmed good. Always report the **± CI**, and rank
> on the metric that matches your question. The `simulating-games` skill states
> these guardrails in full.

## Deck sideboards

A deck can carry **sideboard** cards alongside its maindeck. Every `DeckCard` has a
`role`: unset (the default) for the maindeck, `commander` for a commander, and
`sideboard` for a boarded card. The three sets partition the deck — a card is in
exactly one — and basic lands stay as count fields on the deck.

- **Airtable** — the maindeck links live in the deck's `Cards` field and sideboard
  cards in a `Sideboard` linked-records field (auto-tolerated if the column is
  absent). Basic-land quantity and per-card counts are preserved on save; the
  deck-shrink safety guard measures the **maindeck** only, so boarding cards never
  masks a maindeck that quietly lost cards.
- **Local YAML** — each card row carries its `role`, so sideboards round-trip
  through the local backend identically to Airtable (a legacy deck with no roles
  loads as an all-maindeck deck).
- **Export** — `simulate` and the `forge_dck` exporter render sideboard cards into
  the `.dck` `[Sideboard]` section; the sim itself plays the **maindeck** only.

Role is validated on load (an unknown value is rejected, not silently misfiled), and
a deck with no sideboard renders byte-identically to one built before the feature.

## How it works

- **Card data** — Scryfall, offline-first via a local DuckDB "medallion" lake
  (`raw → normalized → marts`), with a paced live fallback for cache misses.
- **Collection** — a local decks store (DuckDB) fronts your source of record
  (`collection/` YAML by default, or Airtable when configured): reads are served
  from a cached copy and pulled current on a short TTL; edits commit through to the
  source. Decks bind to a **stable identity** (an in-file `uuid` for YAML, the
  record id for Airtable), so renames and duplicate names never mis-target.
- **Analysis** — a neutral fact sheet (curve, pips, ramp, interaction, otag
  buckets) plus otag-informed strategy fit; the skills own the judgment calls.
- **Simulation** — deck → Forge `.dck` → a governed pool of headless Forge JVMs →
  parsed win-rate + telemetry, cached in DuckDB so an unchanged matchup never
  re-runs.

Env vars:

- `MAKE_MAGIC_BACKEND` (`local` | `airtable`) — collection backend.
- `MAKE_MAGIC_DATA_DIR` — local store / cache location (lake, DuckDB, collection
  YAML, and the fetched Forge install all live here).
- `AIRTABLE_API_KEY` — Airtable mode only.
- `MAKE_MAGIC_FORGE_HOME` — path to an existing Forge install (skips the fetch).
- `MAKE_MAGIC_JAVA` — path to an existing `java` binary (skips the JRE fetch).

---

## Upgrading to 0.6.1

The deckbuilding capability was reworked to run through the local decks store
(above). The upgrade from 0.6.0 is a **non-breaking patch** — non-destructive — but note:

- **First run auto-migrates, once.** A stable `uuid` is injected into each local
  `collection/decks/*.yaml` file (an additive field with a "do not edit" comment;
  the write is atomic). No cards, quantities, strategies, or Airtable records are
  changed. If you keep your `collection/` under version control or want belt-and-
  braces, commit/back it up before the first run.
- **No Airtable schema change** — binding uses the existing record id.
- **Edits are now guard-enforced.** Operations that used to slip through silently
  (e.g. `deck-remove --qty -1`, cutting the sole commander, writing to a deleted
  source) are now **refused** with a clear message. This is stricter, not lossy.
- **`list-decks` gained status markers** (`[synced]` / `[ephemeral]` /
  `[synced,source-missing]`); adjust any script that parsed its output.
