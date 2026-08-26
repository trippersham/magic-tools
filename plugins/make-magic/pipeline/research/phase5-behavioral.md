# Phase 5 — behavioral verification (real JVM)

## Review re-verify: real-fire macro gate keys on `MACRO_FIRE_REAL` (2026-08-26)

### The bug being closed (Fix 1, MAJOR)
The proactive macro-fire gate keyed on `DRIVER_MACRO_FIRED`, the emitter-injected marker at
the TOP of the quad macro's `apply()`. But patch 0003's seam calls `apply()` on THROWAWAY AI
search copies at every node where the precondition P holds (`ComputerPlayer6` in-search hook,
~line 566) AND in the real `act()` commit path (~line 175). So `DRIVER_MACRO_FIRED` proves the
macro is REACHABLE in search — NOT that it executed to win. Patch 0003's real `act()` commit
path emits a DISTINCT marker `MACRO_FIRE_REAL name=<getName()> turn=<n>` (followed by
`MACRO_FIRE_DONE name=<...> gameOver=<...>`), baked into the frozen dist jar.

### Fix
- `driver_gate.py`: the proactive gate now requires `MACRO_FIRE_REAL` (true execution) for PASS.
  `DRIVER_MACRO_FIRED` is still parsed but recorded as `GateResult.macro_reachable`
  (reachability), distinct from `macro_fired` (now = real execution).
- Corrected the three mis-documented "apply() invoked ONLY to execute the win" comments
  (`driver_gate.py`, `driver_authoring.py` emitter, `JelevaThoracleReferenceDriver.java`).
- Fix 2 (MAJOR): `_MIN_GATE_GAMES = 12`; `gate_driver` raises `ValueError` (loud) for
  `games < 12`, both modes (the seedless never-slower check flakes when underpowered).
- Fix 3 (MINOR): `drivers.DriverMeta.from_json` fallback for a legacy meta lacking `gate_mode`
  is now the `'unknown'` sentinel (was an arbitrary `'proactive'` guess; the field is
  descriptive-only — `driver_valid` does not branch on it).

### Confirming `MACRO_FIRE_REAL` is real (frozen dist)
`grep -a -o` on the extracted `mage/player/ai/ComputerPlayer6.class` from the worktree
`xmage-dist/target/make-magic-xmage-dist.jar` shows `MACRO_FIRE_REAL name=`, `MACRO_FIRE_DONE`,
and `MACRO FIRE (real)` baked in. (The stale `~/.local/share/make-magic` store jar predates the
marker — a different SHA — so the live runs below used the worktree jar via
`MAKE_MAGIC_XMAGE_DIST_JAR`.)

### Method
Env: `MAKE_MAGIC_XMAGE_DIST_JAR` = worktree `xmage-dist/target/make-magic-xmage-dist.jar`,
`MAKE_MAGIC_JAVA` = JDK-21 JRE, `MAKE_MAGIC_ECJ_JAR` = ecj-3.46.0.jar,
`MAKE_MAGIC_DATA_DIR` = `~/.local/share/make-magic` (warm card DB), `MAKE_MAGIC_XMAGE_HOME`
unset (dist-override). ECJ-compiled each quad via `driver_compile.compile_for_injection`;
ran solo own-turn goldfish through the production launch path.

### Step 1 — `MACRO_FIRE_REAL` appears in a real Jeleva reference goldfish
12-game Jeleva reference-driver (`JELEVA_QUAD_SPEC`) goldfish, raw stderr grep:

```
MACRO_FIRE_REAL count=2
  MACRO_FIRE_REAL name=PlayerA turn=13
  MACRO_FIRE_REAL name=PlayerA turn=19
  MACRO_FIRE_DONE name=PlayerA gameOver=true
  MACRO_FIRE_DONE name=PlayerA gameOver=true
```

The `gameOver=true` on the DONE line confirms the macro really executed to WIN (not merely
reachable). (A parallel 3-game sample fired 0× — small-sample variance, and exactly the reason
for the `_MIN_GATE_GAMES = 12` floor.)

### Step 2 — gate the Jeleva reference quad at games=12 → PASS
```
GATE: passed=True macro_fired(real)=True macro_reachable=True reason=''
driven_median=10.0 baseline_median=13.0
```
PASS on the real-fire marker; never-slower also holds (driven 10.0 <= baseline 13.0 + tol).

### Step 3 — INERT-but-reachable proactive quad → gate FAILS (the new teeth)
Constructed a proactive quad whose macro `applicable()` returns `game.isSimulation()` — TRUE
only on AI search copies, FALSE on the real game — with a no-op `apply()` that wins nothing.
The in-search hook enters `apply()` on throwaway copies (emitting `DRIVER_MACRO_FIRED`) while
the real `act()` sees `applicable()==false` and never commits (so no `MACRO_FIRE_REAL`, and no
infinite loop). 12-game goldfish raw grep:

```
inert MACRO_FIRE_REAL count: 0            (expect 0)
inert DRIVER_MACRO_FIRED (reachable) count: 20314   (expect >0)
```

Gate at games=12:
```
GATE: passed=False macro_fired(real)=False macro_reachable=True
reason='proactive quad never emitted MACRO_FIRE_REAL over 12 solo games (the deterministic win
never REALLY executed in act() — the macro was REACHABLE (DRIVER_MACRO_FIRED seen in search)
but never committed on the real game)'
```

The old gate (keyed on `DRIVER_MACRO_FIRED`) would have PASSED this inert quad — 20314
reachability hits. The upgraded gate correctly FAILS it and distinguishes reachable-but-inert
(FAIL) from really-executes (PASS). Teeth confirmed.
