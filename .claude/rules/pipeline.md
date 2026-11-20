---
paths:
  - "plugins/make-magic/**"
---
# The make-magic pipeline

These files are the Python package and the plugin that ships it. A change here can break the offline gate, the lockstep release, or the local-first promise that lets the plugin run with no account.

- never degrade the local-first, no-credential default path. make-magic reads card data from Scryfall and holds the collection in a local store with no account, and that is its core promise. keep the local store the default and the Airtable base opt-in.
- must add a test for new behavior. the suite is test-first, and the offline gate is the contract: parsing changes get fixtures, and integrity changes get round-trip tests. run `uv run --extra dev pytest` from `plugins/make-magic/pipeline` before you commit.
- never add a network call or a Forge run to the default suite. the markers `live`, `forge`, `canary`, and `integration` are deselected in `pyproject.toml` because they need a key, a JVM, or the network. put a test that needs one of those behind its marker.
- keep the plugin version in `plugins/make-magic/.claude-plugin/plugin.json` equal to the package version in `plugins/make-magic/pipeline/pyproject.toml`. they ship as one lockstep version, and `tests/test_versioning.py` fails on a difference.
- never commit a stale harness jar. the jars under `pipeline/sim/java/forge-simai/` and `pipeline/sim/java/xmage/` are compiled from the sources beside them, and CI rebuilds each from source and fails when the committed bytecode differs. after you edit a harness source, rebuild and commit the jar in the same change.
- never change a runtime-fetched pin without re-pinning its checksum. `forge_runtime.py` pins the Forge tarball by SHA-256, and `xmage_runtime.py` pins the XMage dist; a version bump that skips the checksum lets an unverified download run. CONTRIBUTING holds the bump steps.
- write comments and docstrings for the next developer: say what the code does and why it is built this way. never narrate the code's past (no "previously", "used to", "changed to", "now we"), because the git history holds the evolution and the comment holds the current intent.
- keep the docstring density high, and record the why behind an empirically-derived constant. a Forge or XMage constant comes from measured games, and a reader cannot re-derive it from the value alone.
