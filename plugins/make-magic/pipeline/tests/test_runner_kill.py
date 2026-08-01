"""OFFLINE test that the external timeout reaps the WHOLE JVM process group.

No Forge / no JVM: ``java`` is a tiny shell script that spawns a background
grandchild (the shape ``xvfb-run java …`` takes on Linux — the JVM is a
grandchild of the process the runner spawned) and then sleeps past the external
timeout. A plain ``proc.kill()`` on timeout kills only the direct child and
LEAKS the grandchild; the runner must kill the process group so nothing
survives the external kill-switch.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from pipeline.sim import runner as runner_mod
from pipeline.sim.forge_runtime import ForgeInstall
from pipeline.sim.runner import ForgeError, run_matchup


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_external_timeout_kills_grandchild_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decks_root = tmp_path / 'decks'
    monkeypatch.setattr(ForgeInstall, 'decks_dir', property(lambda self: decks_root))
    # Shrink the one-time-load headroom so the external timeout fires in ~1s.
    monkeypatch.setattr(runner_mod, '_JVM_LOAD_HEADROOM_S', 0)

    pid_file = tmp_path / 'grandchild.pid'
    fake_java = tmp_path / 'fake_java'
    fake_java.write_text(
        '#!/bin/sh\n'
        'sleep 300 &\n'
        f'echo $! > "{pid_file}"\n'
        'sleep 300\n'
    )
    fake_java.chmod(0o755)

    install = ForgeInstall(forge_dir=tmp_path, jar=tmp_path / 'forge.jar', java=fake_java)
    with pytest.raises(ForgeError, match='timeout'):
        run_matchup(install, ('A', '[Main]\n'), ('B', '[Main]\n1 X\n'), n=1, seed=1, timeout_s=1)

    assert pid_file.is_file(), 'fake java never started'
    grandchild = int(pid_file.read_text().strip())

    # The group kill is synchronous, but give the OS a moment to reap.
    deadline = time.monotonic() + 3.0
    leaked = _alive(grandchild)
    while leaked and time.monotonic() < deadline:
        time.sleep(0.05)
        leaked = _alive(grandchild)
    if leaked:  # don't leave the sleeper behind when the assertion fails
        os.kill(grandchild, signal.SIGKILL)
    assert not leaked, f'grandchild {grandchild} survived the external timeout kill'
