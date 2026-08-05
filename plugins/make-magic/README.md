# make-magic

Build and tune **Magic: The Gathering** decks, track your collection, and see how a
deck *actually plays* — all by talking to Claude Code in plain English. Powered by
**Scryfall**, with an **optional** shared Airtable base.

> **You don't run any commands.** Just ask. make-magic gives Claude the skills to
> reason about strategy and card fit, and to play real AI-vs-AI games behind the
> scenes. Everything below is a conversation.

> **Works out of the box — no account, no credential.** Card data comes from
> Scryfall; your collection lives on your machine by default. Point it at a shared
> Airtable base only if you want to (see [Your collection](#your-collection)).

Runs on **macOS / Linux** (on **Windows use WSL2**). Nothing to install beyond the
plugin — it self-provisions everything it needs on first use.

---

## Install

```bash
claude plugin marketplace add trippersham/magic-tools
claude plugin install make-magic@magic-tools
```

Then `/reload-plugins` (or restart Claude Code).

- **Desktop:** Settings → Plugins → Add marketplace `trippersham/magic-tools` → install `make-magic`.
- **Web (Cowork):** this repo's settings enable the plugin automatically — nothing to install.

To confirm it's live, just ask: **"is make-magic working?"** — Claude runs a zero-setup
check and reports back.

---

## Just ask

You talk; make-magic does the work. A few things you can say:

**Build & improve decks**
- *"Build a Krenko goblins deck."*
- *"Optimize my Ozai deck."* / *"What should I add to it from the latest set?"*
- *"Is Sol Ring good in my Krenko deck?"* / *"Should I run [card] in [deck]?"*
- *"Diagnose my deck — what's it missing? Is it too glass-cannon?"*
- *"Propose a few swaps to make it faster, and explain the tradeoffs."*

**See how it plays**
- *"How does my Krenko deck actually play? Simulate it."*
- *"A/B these two versions over ~300 games and tell me which is better."*

**Manage your collection**
- *"Add Sol Ring to my inventory."* / *"I picked up a foil Ragavan."*
- *"Track Ragavan as a card I want."*
- *"Is trading my [X] for [Y] a good deal for my Krenko deck?"*

Claude picks the right skill for each request and does the analysis — you never touch a
command line.

---

## What it can do

| You ask about… | make-magic… |
|---|---|
| **Building a deck** | Guides the whole build — strategy → diagnosis → upgrades → playtest → commit — asking for your input at the right moments. |
| **A deck's game plan** | Writes a clear **Strategy**: how it wins and what a card must do to earn a slot. |
| **What's wrong with a deck** | Diagnoses it from a neutral fact sheet (curve, ramp, interaction, balance) and tells you where it's weak. |
| **Upgrades & swaps** | Proposes **ranked, size-preserving swaps** grounded in the deck's strategy, and applies the ones you accept. |
| **How a deck performs** | Plays **real, rules-enforced AI-vs-AI games** and reports a win-rate (with a confidence interval) and a play-style profile. |
| **Cards you want** | Tracks and prioritizes your chase list. |
| **Your collection & trades** | Adds owned cards (looked up live from Scryfall) and vets trades for fit and value. |

---

## Building a deck

Ask to build or improve a deck and Claude runs a guided flow — frame the **strategy**,
**assess** the current list against a neutral fact sheet, **refine** with ranked swaps,
**validate** by simulating, and **commit** when you're happy. It resumes correctly even
if you come back to it later.

A couple of things that make it safe and pleasant:

- **You can experiment without risk.** Claude can spin up a throwaway *draft* copy of a
  deck, try ideas on it, and only fold the good ones back into your real deck when you
  approve — your saved deck is never touched until you say so.
- **It can't build an illegal deck.** Singleton rules, exactly one commander, sensible
  quantities, and size-preserving swaps are enforced automatically — a bad edit is
  refused with a plain-English reason, never applied silently.
- **Nothing is lost.** Every change is reversible (*"undo that last swap"*), and Claude
  tracks whether a deck's assessment or last playtest is still current so it knows what
  to re-check.

---

## Seeing how a deck plays

Ask *"how does this deck actually play?"* and make-magic runs real games in
[MTG Forge](https://github.com/Card-Forge/forge) — a rules-enforced engine — against a
field of opponents, then reports a **win-rate ± confidence interval** and a numerical
profile (how fast it wins, how it wins, its ramp curve).

**No setup on your part.** The first time you simulate, make-magic downloads a pinned
Forge release and (if needed) a Java runtime — a **one-time ~350 MB download**,
checksum-verified and cached, from the official sources (nothing is bundled or
redistributed). In an interactive session it asks before the big download; you can also
say *"provision Forge now"* to get it out of the way. Already have Forge/Java? make-magic
uses yours.

> **Read the results as directional, not gospel.** Forge's AI is competent at fair
> beatdown and midrange but weak at control/combo/stax. A deck that *can't* beat the
> field is genuinely flawed; one that *beats* it is confirmed functional, not confirmed
> *good*. Claude always reports the confidence interval and picks the metric that matches
> your question. For a tight verdict, budget ~300 games per deck.

---

## Your collection

By default your collection lives on your machine — no account, nothing to set up. Claude
looks cards up from Scryfall (offline-first, with a live fallback), so it works on day one.

**Prefer a shared, multi-device Airtable base?** Opt in by telling Claude to *"use the
Airtable backend,"* and provide an Airtable **Personal Access Token** as `AIRTABLE_API_KEY`
(a read-scoped token limited to your base is best — grant only `data.records:read` /
`schema.bases:read`). Set it in your environment, or in a gitignored `.env` next to the
plugin (copy from `.env.example`); on Cowork, use the environment's **Environment
Variables** field.

Your decks bind to a **stable identity** (an internal id, not the name), so renaming a
deck never loses the link, and make-magic never creates or deletes an Airtable record
except through an explicit, guarded save.

A deck can also carry a **sideboard** (cards alongside the maindeck); it round-trips
through both local and Airtable storage and exports into Forge's `[Sideboard]` section,
while the sim plays the maindeck.

---

## How it works

- **Card data** — Scryfall, offline-first via a local data lake, with a paced live
  fallback for anything not yet cached. No bulk download needed to get started.
- **Your collection** — a local working copy fronts your source of record (files on your
  machine by default, or Airtable): reads are fast and cached, edits commit through, and
  everything is bound by a stable identity so nothing mis-targets.
- **Analysis** — a neutral fact sheet plus strategy-aware fit; Claude owns the judgment
  calls.
- **Simulation** — your deck → Forge → a pool of headless game engines → a win-rate and
  play-style profile, cached so an unchanged matchup never re-runs.

---

## Driving it directly (advanced)

You don't need this — the skills are the interface, and they drive it for you. But make-magic
is backed by two scriptable CLIs under `${CLAUDE_PLUGIN_ROOT}/scripts/` if you want to build
on top or automate: **`collection`** (decks, inventory, chase, trades) and **`simulate`**
(Forge games). They behave identically on local files or Airtable. Every verb takes `-h`.

```bash
C="${CLAUDE_PLUGIN_ROOT}/scripts/collection"
S="${CLAUDE_PLUGIN_ROOT}/scripts/simulate"

"$C" status                                     # backend + readiness (zero setup)
"$C" get-deck "Krenko Goblins" [--provenance]   # deck JSON (+ assessment/sim freshness)
"$C" list-decks                                 # decks + status: [synced] / [ephemeral]
"$C" factsheet "Krenko Goblins"                 # neutral curve / ramp / interaction analysis

# Guarded, quantity-aware edits (a bad edit is refused with a clear message, not applied)
"$C" deck-swap "Krenko Goblins" --add "Goblin Recruiter" --cut "Lightning Bolt" --why "…"
"$C" deck-add / deck-remove / set-strategy / set-assessment / undo-deck   # …see -h

# Experiment safely: a local draft copy, promoted back when good (the copy auto-retires)
"$C" new-draft "Krenko (explore)" --from "Krenko Goblins"
"$C" promote-deck "Krenko (explore)" --to "Krenko Goblins"

# Collection + simulation
"$C" add-card "Sol Ring"                         # --foil is a COUNT, not true/false
"$S" doctor [--provision]                        # Forge/Java status (or fetch the one-time ~350 MB)
"$S" deck "Krenko Goblins" --gauntlet guilds --games 30    # --games is PER opponent
```

Notes worth knowing: decks are addressed **by name** (`--id <prefix>` disambiguates if a
name repeats); the store **enforces every deck invariant** so a bad edit is refused rather
than silently applied; error messages name the exact safe command to run next; and drafts
stay local until you `promote-deck`. The commander is a `cards[]` entry with
`"role": "commander"` — not a top-level field.

---

## Upgrading to 0.6.1

The deckbuilding capability was reworked to run through a local working-copy store. The
upgrade from 0.6.0 is a **non-breaking patch** — non-destructive — but note:

- **First run auto-migrates, once.** A stable id is added to each local deck file (an
  additive field; the write is atomic). No cards, quantities, strategies, or Airtable
  records change. If you keep your collection under version control, commit/back it up
  first for peace of mind.
- **No Airtable schema change.**
- **Deck edits are now guard-enforced** — operations that used to slip through silently
  (removing the sole commander, negative quantities, writing to a deleted source) are now
  refused with a clear message. Stricter, not lossy.

Full details in the [CHANGELOG](../../CHANGELOG.md).
