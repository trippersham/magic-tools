# Phase 3 — behavioral verification (real JVM)

## Review re-verify: dist-override `resolve()` prepends the harness jar (2026-08-25)

### The bug being closed
In dist-override mode (`MAKE_MAGIC_XMAGE_DIST_JAR` set), `xmage_runtime.resolve()`
returned `classpath = str(override)` — the dist jar ALONE. The dist jar bundles a STALE
shaded `org/makemagic/xmage/XMageBatch.class` that has the `makemagic.driver` string but
NO `DRIVER_REGISTERED` emit (verified: `grep -a -o` on the extracted class shows the
harness copy carries both markers, the dist copy only `makemagic.driver`). With the dist
alone on the classpath the stale class wins class-loading, ignores `-Dmakemagic.driver`,
and the run degrades to silent pure CP7 with exit 0 — a driver gate run in override mode
would score every driver deck as bare CP7 and pass it as "no regression."

### Fix
- `resolve()` override branch: `classpath = os.pathsep.join((str(_HARNESS_JAR), str(override)))`
  (mirrors the reactor branch), so the committed `make-magic-xmage.jar` (fresh
  register-by-playerId XMageBatch) shadows the stale dist class.
- Defense-in-depth: `_launch_xmage` now RAISES `XMageError` when a driver was requested
  (`driver is not None`) but the captured stdout contains no `DRIVER_REGISTERED` line.

### Method
Ran a 3-game Jeleva solo goldfish WITH the reference Driver
(`makemagic.driver.reference.JelevaThoracleReferenceDriver`) through the PRODUCTION launch
path — `xmage_runtime.resolve()` (dist-override) → `_compose_launch_cmd` → `_launch_xmage`
— not a manually-composed classpath. ECJ-compiled the Driver via
`driver_compile.compile_for_injection`. cwd = the warm `<data_dir>/xmage` card DB.

Env: `MAKE_MAGIC_XMAGE_DIST_JAR` = the local `xmage-dist/target/make-magic-xmage-dist.jar`,
`MAKE_MAGIC_JAVA` = JDK-21 JRE, `MAKE_MAGIC_ECJ_JAR` = ecj-3.46.0.jar,
`MAKE_MAGIC_DATA_DIR` = `~/.local/share/make-magic`.

### BEFORE (pre-fix `resolve()` output — dist jar ALONE)
Reconstructed the pre-fix classpath (`classpath = DIST` only) and ran the driver-injected
goldfish. The run emitted NO `DRIVER_REGISTERED` line (matching the reviewer's Arm A = 0) —
observed here as the new fail-loud guard RAISING:

```
XMage jeleva goldfish [BEFORE ...]: driver makemagic.driver.reference.JelevaThoracleReferenceDriver
was requested (-Dmakemagic.driver) but the run emitted no DRIVER_REGISTERED line — the Driver
never registered on PlayerA (a shadowed/stale XMageBatch that ignores the seam ...).
```

### AFTER (production `resolve()` — harness jar prepended)
`resolve().classpath[0]` == the committed harness jar; the run registers and fires:

```
exit=0
classpath[0]= .../pipeline/sim/java/xmage/make-magic-xmage.jar
DRIVER_REGISTERED fqcn=makemagic.driver.reference.JelevaThoracleReferenceDriver playerId=4dfb6113-...
JELEVA_REFERENCE_DRIVER_REGISTERED playerId=4dfb6113-... (Phi=DriverBonus + macro=MacroRegistry + S=SelectionRegistry)
SELECTION_STEER_FETCH pid=... fetched=Thassa's Oracle tutor=Gamble ...
GOLDFISH SUMMARY (OWN TURNS) ... variant=...JelevaThoracleReferenceDriver games=3 kills=3 medianKillsOwn=12.0 ...
```

`DRIVER_REGISTERED` + the macro/steer-fire lines (`SELECTION_STEER_FETCH`) now appear where
they did NOT before the fix. The production path (via `resolve()`) registers the Driver.
