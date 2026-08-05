# make-magic — for agents

You are operating make-magic on a user's behalf. **The skills are your interface.** They
own the judgment — strategy fit, card evaluation, diagnosis, and the simulation
guardrails — and they drive the underlying CLIs correctly for you. Reach for the raw CLI
only as a **backup**: when a skill can't be invoked, when you're scripting something a
skill doesn't cover, or when you need a single low-level verb a skill would otherwise call.

Do **not** hand-roll deck logic (swaps, legality, sizing, sync) in the shell — the skills
and the store already enforce it, and doing it by hand is how you introduce the exact bugs
this system was built to prevent.

---

## Use the skills first

| Skill | Invoke it when the user wants to… |
|---|---|
| **building-decks** | build, optimize, or evaluate a deck; the orchestrator that runs the whole guided flow and delegates the stages below. |
| **distilling-strategy** | author or refresh a deck's **Strategy** (game plan + what makes a card fit). |
| **assessing-decks** | diagnose a deck / balance (Quadrant Theory) from a neutral fact sheet, and write its **Assessment**. |
| **refining-decks** | get ranked, size-preserving upgrade swaps grounded in the strategy. |
| **simulating-games** | play AI-vs-AI games and report win-rate ± CI + telemetry. |
| **chasing-cards** | track/prioritize wanted cards. |
| **managing-inventory** | add owned cards, vet trades. |

If a request matches a skill, invoke the skill — it will use the CLI below correctly. The
CLI section exists so you can operate when a skill isn't available, and so you understand
what the skills are doing.

---

## The CLI (backup)

Two backend-agnostic CLIs under `${CLAUDE_PLUGIN_ROOT}/scripts/`: **`collection`** (decks,
inventory, chase, trades) and **`simulate`** (Forge games). Every deck read/edit/lifecycle
step goes through `collection` and behaves identically on local files or Airtable. Run any
verb with `-h` for its flags.

```bash
C="${CLAUDE_PLUGIN_ROOT}/scripts/collection"
S="${CLAUDE_PLUGIN_ROOT}/scripts/simulate"

"$C" status                 # {"backend": "local"|"airtable", ...} — verifies the pipeline loaded (zero setup)
```

### Deck workflow

```bash
# Read (name-addressed; --id disambiguates if a name is not unique)
"$C" get-deck "Krenko Goblins"                 # full deck JSON incl. cards[]
"$C" get-deck "Krenko Goblins" --provenance    # + assessment/sim freshness: fresh | stale | absent
"$C" list-decks                                 # decks + status: [synced] / [ephemeral] / [synced,source-missing]
"$C" factsheet "Krenko Goblins"                 # neutral curve / ramp / interaction / otag analysis
"$C" deck-combos "Krenko Goblins"               # named-card combos present (archetype-fidelity signal)

# Author strategy / assessment / focus (these commit through on a synced deck)
"$C" set-strategy "Krenko Goblins" "Go wide on goblins, then alpha strike."
"$C" set-assessment "Krenko Goblins" "Fast but fragile; light on interaction."

# Edit (typed, guarded, quantity-aware; --why is logged to the ledger)
"$C" deck-swap "Krenko Goblins" --add "Goblin Recruiter" --cut "Lightning Bolt" --why "tutor density"
"$C" deck-add "Krenko Goblins" "Goblin Chieftain"
"$C" deck-remove "Krenko Goblins" "Lightning Bolt"
"$C" undo-deck "Krenko Goblins"                 # step back one edit

# Explore without touching the real deck, then commit back (the copy auto-retires)
"$C" new-draft "Krenko (explore)" --from "Krenko Goblins"
"$C" deck-swap "Krenko (explore)" --add "Skirk Prospector" --cut "Mountain"
"$C" promote-deck "Krenko (explore)" --to "Krenko Goblins"

# Build clean-slate: an ephemeral draft, grown locally, promoted at the end
"$C" new-draft "New Brew" --commander "Krenko, Mob Boss" --format Commander
"$C" deck-add "New Brew" "Goblin Chieftain"
"$C" promote-deck "New Brew" --to "New Brew"

# Record a simulation result (drives sim-freshness); the simulating-games skill calls this
"$C" stamp-sim "Krenko Goblins" --result '{"winrate": 0.42, "games": 300}'
```

### Collection / inventory / chase

```bash
"$C" onboard --backend local            # pin a backend (optional — local is the default)
"$C" add-card "Sol Ring"                 # owned card (hydrated live from Scryfall)
"$C" add-card "Krenko, Mob Boss" --qty 1 --foil 1   # --foil is a COUNT, not true/false
"$C" add-chase "Ragavan, Nimble Pilferer"
"$C" save-deck --from-json - <<'JSON'    # author/replace a deck from JSON
{"name": "Krenko Goblins", "cards": [{"name": "Krenko, Mob Boss", "role": "commander"}, {"name": "Goblin Chieftain"}]}
JSON
```

### Simulation

```bash
"$S" doctor                              # Forge/Java availability + safe JVM pool size (offline, no game)
"$S" doctor --provision                  # fetch Forge + JRE now (one-time ~350 MB; --yes to skip the prompt)
"$S" deck "Krenko Goblins" --gauntlet guilds --games 30     # --games is PER opponent
"$S" ab "Krenko Goblins" variant.dck --gauntlet curated --games 30
"$S" gauntlet show --source guilds
```

---

## Rules that keep you (and the user's data) safe

The operator here is an agent that runs advice literally — so the system is built to be
safe *by construction*. Rely on it, and follow these:

- **The store enforces every invariant; a bad edit is REFUSED, not applied.** Singleton,
  single-commander (no cutting the sole commander, no commander at qty 2), quantity ≥ 1,
  size/shrink guard, size-preserving commander-safe swaps. If a `collection` edit exits
  non-zero, read the message — it is a plain-English refusal, not a crash to work around.
- **Error messages are an executable API. Follow them literally.** Every message names
  only real, safe commands. If it says *"re-save it with `collection save-deck "X"`,"*
  run exactly that. Never invent a recovery step, and never reconstruct a deck from a
  file you weren't pointed at.
- **Address decks by name.** Names may repeat; on an ambiguity refusal, re-run with the
  `--id <prefix>` it prints. Don't guess.
- **Ephemeral vs synced.** A draft (`new-draft`) is local-only until you `promote-deck`
  it. To try things without risk, work on a `new-draft --from "<deck>"` copy — edits stay
  local until promotion. `set-*`/`deck-*` on a synced deck commit through to the source.
- **A gone source refuses writes; recover with a fresh save.** If a deck's source was
  deleted or re-identified, edits refuse with *"source … is gone."* Recovery is
  `collection save-deck "<name>"` (bare name) — it writes a fresh copy safely and can't
  overwrite an unrelated file. `get-deck "<name>" --local` shows your local copy.
- **Never make an unrequested production Airtable write.** The base is a shared source of
  record. Author/edit only on the user's explicit request; verification is read-only.
- **Simulation is directional, not ground truth.** Forge's AI is weak at
  control/combo/stax. Always report the ± CI; a deck that beats the gauntlet is *functional*,
  not *good*. Budget ~300 games per finalist for a real verdict. Retain logs
  (`simulate log`) — Forge's RNG seed is not reproducible.
- **Commander is a `cards[]` entry with `"role": "commander"`** — not a top-level field.
  A card is in exactly one of maindeck / commander / `"role": "sideboard"`.

---

## Configuration (env vars)

- `MAKE_MAGIC_BACKEND` (`local` | `airtable`) — collection backend. Resolution:
  explicit `MAKE_MAGIC_BACKEND` → onboarded choice → `AIRTABLE_API_KEY` present → `local`.
- `MAKE_MAGIC_DATA_DIR` — local store / cache location (data lake, DuckDB, collection
  files, and the fetched Forge install all live here). Set it to relocate or isolate.
- `AIRTABLE_API_KEY` — Airtable mode only (a read-scoped PAT limited to the base).
- `MAKE_MAGIC_FORGE_HOME` — an existing Forge install (skips the fetch).
- `MAKE_MAGIC_JAVA` — an existing `java` (skips the JRE fetch).

`uv` and Java self-provision on session start / first sim — you do not need to install
either.
