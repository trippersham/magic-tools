# make-magic

Magic: The Gathering deck building, card chasing, and inventory management for
Claude Code — backed by a shared Airtable base and Scryfall.

Skills included: **building-decks**, **chasing-cards**, **managing-inventory**.

The skills, commands, and behavior are identical no matter how you install. Only
the setup steps differ per platform.

---

## Prerequisites

- **Node.js** (18+). The bundled Airtable MCP server runs on Node. On Claude
  Code on the web ("Cowork") Node is already installed, so there's nothing to do
  there.
- **`uv`** — used by the Python helper scripts. You do **not** need to install
  it: the plugin self-provisions `uv` on session start (a `SessionStart` hook
  runs `ensure-uv.sh`, which fetches a pinned `uv` into a local cache if it isn't
  already on your PATH). macOS/Linux only.
- Nothing else. No global installs, no build step.

Supported OS: **macOS / Linux** (Windows is not supported).

---

## Install

Pick your target below. You need to do two things everywhere: **install the
plugin** and **set the `AIRTABLE_API_KEY` credential**.

### Tab: Claude Code (CLI)

**1. Add the marketplace, then install the plugin.** From the Claude Code TUI
(slash commands) or your shell (`claude ...`):

```bash
claude plugin marketplace add trippersham/magic-tools
claude plugin install make-magic@magic-tools
```

(Equivalently, from inside the TUI: `/plugin marketplace add trippersham/magic-tools`
then `/plugin install make-magic@magic-tools`.)

**2. Reload.** If you installed from the TUI, run `/reload-plugins`, or restart
Claude Code.

**3. Set the credential.** See [Credential](#credential) below — locally you use
a gitignored `.env` file or a shell export.

### Tab: Claude Code (Desktop)

**1. Open Settings** from the profile menu.

**2. Go to Plugins → Add → Add marketplace** and enter:

```
trippersham/magic-tools
```

**3. Install `make-magic`** from the plugin list for the `magic-tools`
marketplace.

The CLI commands work here too if you have a terminal:

```bash
claude plugin marketplace add trippersham/magic-tools
claude plugin install make-magic@magic-tools
```

**4. Set the credential.** See [Credential](#credential) below — a gitignored
`.env` file or a shell export.

### Tab: Claude Code on the web ("Cowork")

**1. Enablement is already committed.** This repository ships a
`.claude/settings.json` that registers the `magic-tools` marketplace and enables
`make-magic`, so a Cowork session that opens this repo loads the plugin at
startup. You do **not** run `plugin marketplace add` / `plugin install` in
Cowork.

**2. Set the credential** in the environment's **Environment Variables** field:

```
AIRTABLE_API_KEY = <your Airtable token>
```

See [Credential](#credential) for what token to use.

---

## Credential

The Airtable MCP server needs `AIRTABLE_API_KEY` — an Airtable **Personal Access
Token** that can read the owner's shared "Magic Inventory" base. The base data is
already populated and shared; you don't create or seed anything, you just need a
token with read access to it.

**Scope the token narrowly.** The bundled MCP server exposes write tools
(create/update/delete records, create/update tables and fields, `upload_attachment`),
so a read-only workflow is best served by a **read-scoped, minimal PAT** — grant only
the `data.records:read` / `schema.bases:read` scopes and limit it to the "Magic
Inventory" base. That way the token's scope, not the tooling, bounds the blast radius.

Resolution order (see `.mcp.json`):

1. An ambient `AIRTABLE_API_KEY` in the environment always wins.
2. Otherwise, if a gitignored `plugins/make-magic/.env` exists, it is sourced.

### Local (CLI / Desktop)

Create a gitignored `.env` next to the plugin (copy from `.env.example`):

```bash
# plugins/make-magic/.env   (gitignored)
AIRTABLE_API_KEY=your_token_here
```

Or export it in your shell before starting Claude Code:

```bash
export AIRTABLE_API_KEY=your_token_here
```

### Cowork

Set `AIRTABLE_API_KEY` in the environment's **Environment Variables** field (see
the Cowork install tab above). Do not commit tokens.

### Optional: 1Password (local, macOS/Linux)

Instead of pasting a raw token into `.env`, you can resolve it at load time from
the 1Password CLI. Put this one line in the gitignored `plugins/make-magic/.env`
(placeholders only — substitute your own account, vault, and item):

```bash
export AIRTABLE_API_KEY="$(op read --account <your-1p-account> 'op://<Vault>/<Item>/credential')"
```

The `.env` file is gitignored, so the real `op://` path stays out of the repo.

---

## Verify it works

After install + credential, start a session in a checkout of this repo and try a
skill, e.g.:

```
Show me what's in my Magic inventory.
```

This exercises the Airtable MCP (proving the bundled server booted) and the helper
scripts (proving `uv` self-provisioned).

If the Airtable MCP fails to start, check that `AIRTABLE_API_KEY` is set for your
platform per [Credential](#credential) above.
