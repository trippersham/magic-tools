---
paths:
  - "**/pipeline/transforms/strategy/config.py"
---

# Strategy configuration

Configuration is universal or gone: a boolean knob is a frozen branch, not a choice.

<config-universal-or-gone>

- Do not add a boolean knob to `StrategyConfig` for behavior that is always on (or always off) in production. A frozen toggle is dead config plus an un-run branch. Instead: inline the behavior at its site (ADR 0001 §3a). See `plugins/make-magic/docs/adr/0006-config-universal-or-gone.md`. Check: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` + the parity oracle.
- Thresholds (tunable VALUES — `gy_*`, `conf_*`, `identity_*`, `cmd_weight`, …) stay; booleans (frozen BRANCHES) go. All strategy boolean knobs have been removed (the last, `enable_tutor_land_enabler`, went with the F2 rework — its lands signal is now an `otag:tutor-land` query); only tunable thresholds remain in `StrategyConfig`.
- If you change the knob set, update ADR 0006 in the same change and re-verify its Test.

</config-universal-or-gone>
