---
paths:
  - "**/pipeline/transforms/strategy/data/attribute_catalog.json"
  - "**/pipeline/transforms/strategy/data/event_catalog.json"
  - "**/pipeline/transforms/strategy/strategy_graph.py"
  - "**/pipeline/transforms/strategy/query.py"
  - "**/pipeline/transforms/strategy/distill_strategies.py"
  - "**/pipeline/transforms/strategy/tests/fixtures/distill_parity_baseline.json"
---

# Byte-parity held files

These curated files fix the strategy engine's composite-SHA parity. A silent byte change regresses the validated engine and is indistinguishable from a bug.

<protected-parity>

- Never change these except as a deliberate, proof-carrying re-baseline. Reason: an unexplained SHA shift cannot be told apart from a regression across the corpus. Instead: attach a behavioral-equivalence proof for every output that shifts (e.g. 613=613) and regenerate the parity baselines in the same change. See `plugins/make-magic/docs/adr/0003-byte-parity-held-files.md`. Check: `uv run python -m pipeline.transforms.strategy.tests.engine_parity_oracle` + the parity test asserting `composite_sha`.
- If you change one of these, update ADR 0003 in the same change and re-verify its Test from the regenerated oracle. Reason: the decision's one home is the record.

</protected-parity>
