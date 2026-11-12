# AGENTS.md — make-magic

Agent brief for the make-magic plugin.

## Architecture Decision Records — read before you change

The strategy engine records its durable design decisions in
[`docs/adr/`](docs/adr/README.md), co-located with the code they govern.

- **Before you change a file, read the ADR that governs it** — each record lists its
  *Code paths*.
- **If your change alters the decision, update that record in the same change** and
  re-verify its *Test* from observed behavior.
- Governed files inject their record automatically when you touch them, via repo-root
  `.claude/rules/adr-*.md` (native Claude Code `paths:` rules).

Start at the **[ADR index](docs/adr/README.md)**.
