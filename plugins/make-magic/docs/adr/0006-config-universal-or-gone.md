# 0006 Configuration: universal or gone — frozen boolean knobs are removed, not left as toggles

| Field | Value |
|---|---|
| Status | Accepted. Built in the consolidation. |
| Review | pending owner review: agent-authored; records the collapse as built. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` (the 3 deconf-capability tests were removed with the capability). Output-preservation proven: the hermetic parity oracle (both P0 SHAs unchanged) **and** the full-corpus recalc (`recalc_corpus()`, 170 decks, SHA `8b0125eeb2e1…3554` unchanged pre/post). |
| Code paths | [`config.py`](../../pipeline/pipeline/transforms/strategy/config.py) (the knob set) |

## Context
`StrategyConfig` carried boolean knobs defaulting to the validated prototype's
settings (`GROUPS=all`, `EDHREC_LABELS=on`, `COMBO_OVERRIDE=on`, …). Audit found
**none was ever set to a non-default value in production** — only tests flipped
them. They were frozen prototype-experiment toggles: dead configuration, each
gating an un-run branch.

## Decision
A boolean knob is a *frozen branch*, not a choice (ADR 0001 §3a). Each is removed
and its branch inlined to the validated default:
- `edhrec_labels` → EDHREC identities/synths **always on** (threading removed from
  held `vocab.py`'s build pipeline + `assets.py`/`facts.py`/`substantiveness.py`);
- `combo_override`, `combo_mana_sink`, `ramp_label` → **always on**;
- `ench_label` → `enchantments` is **always** facts-only (not a naming candidate);
- the `deconf_anthem` group + the `anthem-deconf` attribute/routing → **removed**, so
  broad `power-matters`/`toughness-matters` never routes to anthem — the J2/B6
  anti-over-fire fix is now permanent (completed by the A1 vocab fold).

**Thresholds stay** (`gy_*`, `conf_*`, `identity_*`, `typal_dom_share`, `over_t`,
`ctrl_min_cs`, `combo_win_min`, `combo_primary_*`, `cmd_weight`) — a threshold is a
tunable *value*, not a frozen *branch*. `enable_tutor_land_enabler` was **also
removed** by the F2 rework (task 1.3), which inlined its always-on gate and moved the
lands signal onto a pasteable `otag:tutor-land` query — so **every** strategy boolean
knob is now gone; only tunable thresholds remain.

## Reasons
A frozen toggle is dead config plus an un-maintained branch; removing it is
behavior-preserving (prod already sits at the default) and shrinks the surface the
other clauses police. The value/branch test (ADR 0001) is the discriminator.

## Options not taken
- **Keep them configurable.** Rejected: never varied in production; dead config.
- **Field-only removal for `edhrec_labels`/`deconf_anthem`** (remove the field, keep
  the internal threading). Rejected by the owner in favor of full deep-removal with
  a full-corpus parity check — because the hermetic oracle covers only 17 decks
  (ADR 0003's gap), so the deep removal was verified against all 170.

## Consequences
- Output-preserving: hermetic SHAs identical; full-corpus SHA `8b0125ee…3554`
  identical across 170 decks.
- 3 deconf-capability tests removed (`test_j2_anthem_deconf` trimmed to the narrow
  state; the `test_vocab_separation` deconf-gating test deleted).
- All strategy boolean knobs are now removed (incl. `enable_tutor_land_enabler`, via the
  F2 rework); only tunable thresholds remain in `StrategyConfig`.

## Related records
- 0001 — the value/branch test this applies.
- 0003 — held files (`vocab.py`, `facts.py`); this change is output-preserving.
