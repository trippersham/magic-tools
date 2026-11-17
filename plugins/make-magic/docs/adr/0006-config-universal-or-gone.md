# 0006 Configuration: universal or gone — frozen boolean knobs are removed, not left as toggles

| Field | Value |
|---|---|
| Status | Accepted. Built in the consolidation. |
| Review | pending owner review: agent-authored; records the collapse as built. |
| Test | From `plugins/make-magic/pipeline`: `uv run --extra dev pytest pipeline/transforms/strategy/tests -q` (the 3 deconf-capability tests were removed with the capability). Output-preservation proven: the hermetic parity oracle (both P0 SHAs unchanged) **and** the full-corpus recalc (`recalc_corpus()`, 170 decks, SHA `8b0125eeb2e1…3554` unchanged pre/post). |
| Code paths | [`config.py`](../../pipeline/pipeline/transforms/strategy/config.py) (the knob set); [`vocab.py`](../../pipeline/pipeline/transforms/strategy/vocab.py), [`assets.py`](../../pipeline/pipeline/transforms/strategy/assets.py), [`data/attribute_catalog.json`](../../pipeline/pipeline/transforms/strategy/data/attribute_catalog.json) (the `new_buckets` collapse) |

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
  held `vocab.py`'s build pipeline + `assets.py`/`strategy_graph.py`/`substantiveness.py`);
- `combo_override`, `combo_mana_sink`, `ramp_label` → **always on**;
- `ench_label` → `enchantments` is **always** facts-only (not a naming candidate);
- the `deconf_anthem` group + the `anthem-deconf` attribute/distillation → **removed**, so
  broad `power-matters`/`toughness-matters` never distills to anthem — the J2/B6
  anti-over-fire fix is now permanent (completed by the A1 vocab fold).
- the `new_buckets` group → **always on** (the off-path was never selected in
  production, tests, or CLI — a dead `enabled_groups` toggle): its new-bucket
  identities (`food`/`mm-counters`/`theft-newbuckets`) are unconditional in
  `vocab.py` (the `newbuckets`/`arch_newbuckets` plumbing on `build_router`/
  `StrategyAssets` is removed), its bucket slugs are folded into `_v4_base_buckets`
  in `attribute_catalog.json` (removed from `_v4_groups`), and it is dropped from
  `ALL_GROUPS`/`REFINED_GROUPS`.

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
- The later `new_buckets` collapse is likewise byte-parity: the hermetic oracle
  SHAs (engine `7a73aba9…`, resolve `1e54293c…`) and the current full-corpus SHA
  `07eb6673…` are unchanged pre/post.
- 3 deconf-capability tests removed (`test_j2_anthem_deconf` trimmed to the narrow
  state; the `test_vocab_separation` deconf-gating test deleted).
- All strategy boolean knobs are now removed (incl. `enable_tutor_land_enabler`, via the
  F2 rework); only tunable thresholds remain in `StrategyConfig`.

## Amendment — WS1.2: the group MECHANISM is gone (last group knob removed)
The `new_buckets` collapse (above) left the broader **group-merge mechanism** standing —
`enabled_groups: frozenset` on `StrategyConfig`, `_enabled_groups` / `REFINED_GROUPS` /
`ALL_GROUPS`, and the `_v4_base_buckets` + `_v4_groups` merge in `assets.py`. Audit: that
field **never narrowed** from its all-groups default in production, tests, or CLI — the
merge result is a constant, so `enabled_groups` was itself a frozen branch (dead config).

The WS1.2 CardAttribute-model unification (**ADR 0009**) removes the whole mechanism:
`enabled_groups` / `_enabled_groups` / `REFINED_GROUPS` / `ALL_GROUPS` are deleted, and the
group slugs are baked into each bucket's query in the unified `card_attributes` map
(`_v4_base_buckets` / `_v4_groups` / `_v4_new_bucket_names` deleted). Byte-parity: the three
P0 SHAs (engine `7a73aba9…`, resolve `1e54293c…`, full-corpus `07eb6673…`) are unchanged
pre/post. `test_config_settings` drops its `enabled_groups` assertions; no behavior changed.

## Related records
- 0001 — the value/branch test this applies.
- 0003 — held files (`vocab.py`, `strategy_graph.py`); this change is output-preserving.
- 0009 — the CardAttribute-model unification that bakes the group slugs into the query map.
