# magic-tools

A Claude Code plugin marketplace for Magic: The Gathering card inventory management.

## What's Included

- **make-magic plugin**: Manage your MTG card inventory using Airtable as a database and Scryfall for card metadata. Includes workflows for loading decks, recording trades, backfilling card data, and tracking your collection.

## Installation

```bash
claude plugins add https://github.com/trippwickersham/magic-tools
```

## Prerequisites

1. **Enable Airtable connector**: Run `/mcp` in Claude Code, then enable and authenticate with Airtable

2. **Clone Airtable base template**: [Airtable Base Template](https://airtable.com/placeholder-template-link) - duplicate this to your workspace

3. **Install uv**: Required for running the Scryfall batch metadata script
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

## Cardlist Format

Place your deck lists in a `cardlists/` directory in your project. Each file should contain one card per line:

```
<quantity> <card name>
```

Example (`cardlists/modern-burn.txt`):
```
4 Lightning Bolt
4 Goblin Guide
4 Monastery Swiftspear
2 Eidolon of the Great Revel
```

## Usage

Once installed, invoke the skill:

```
/managing-inventory
```

The skill provides workflows for:
- Loading decks from cardlists into Airtable
- Recording trades and acquisitions
- Backfilling Scryfall metadata (prices, sets, images)
- Querying your inventory
