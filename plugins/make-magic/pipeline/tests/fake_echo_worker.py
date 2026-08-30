#!/usr/bin/env python3
"""A fake echo-worker speaking the game-queue protocol — for WorkerPool tests (NO JVM).

It stands in for the Phase-2 ``XMageBatch --worker`` JVM: print ``READY``, read a
``TASK {json}`` line from stdin, (optionally) emit a ``GAME`` heartbeat, sleep, then
print a canned ``RESULT`` + ``READY`` again. Behavior is tuned by env vars so a single
script drives every scheduling/fault scenario:

* ``FAKE_SLEEP_MS``   — per-task work time (default 0).
* ``FAKE_SLOW_FIRST`` — if set, the FIRST task sleeps ``FAKE_SLOW_MS`` (backpressure test).
* ``FAKE_SLOW_MS``    — the slow duration (default 3000).
* ``FAKE_DIE_ON_TASK``— crash (``os._exit``) mid-first-task without emitting RESULT (death test).
* ``FAKE_DIE_ON_TASK_ID`` — crash (``os._exit``) WHENEVER a task with this exact ``task_id`` is
  received (no RESULT). A *poison task* that kills every worker that touches it — drives the
  Phase-3 governor retry-cap test (unlike ``FAKE_DIE_ON_TASK`` this is keyed by id, not order).
* ``FAKE_DIE_ON_SUBJECT`` — crash (``os._exit``) WHENEVER a task's SUBJECT (``task_id`` first
  ``|``-field) equals this value (no RESULT). A *poison subject* whose every game fails forever —
  drives the simd fairness + persistent-quarantine tests (a whole subject that can never complete).
* ``FAKE_RUNLOG`` — a file path; every task run to COMPLETION (RESULT emitted) appends its
  ``task_id`` (O_APPEND, one per line). A cross-restart re-run of a committed game shows the same
  ``task_id`` twice — the simd crash-safe-resume test asserts this never happens (zero re-run).
* ``FAKE_DIE_AFTER_RESULT`` — complete the FIRST task (emit RESULT) then crash BEFORE the
  next READY — i.e. die *between* tasks with no in-flight task (respawn/MINOR-2 test).
* ``FAKE_SILENT``     — accept a task then go silent forever, no heartbeat/RESULT (stall test).
* ``FAKE_SILENT_AFTER_HEARTBEAT`` — accept the first task, emit ONE ``GAME`` heartbeat, then
  go silent forever — so the reader thread is actively mid-stream when the watchdog reaps
  (MAJOR-1 single-owner-pipe test).
* ``FAKE_HEARTBEAT``  — if set, emit a ``GAME`` heartbeat before sleeping.
* ``FAKE_FAULT_ONCE_FILE`` — makes ``FAKE_DIE_ON_TASK``/``FAKE_SILENT`` **one-shot**: the
  first worker to touch this marker path faults; every respawn sees the marker and runs
  normally (the replacement is not itself faulty — real workers are identical).

All timing is coarse; tests assert structural outcomes, never exact timing.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _emit(line: str) -> None:
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


def main() -> None:
    sleep_ms = int(os.environ.get('FAKE_SLEEP_MS', '0'))
    slow_first = bool(os.environ.get('FAKE_SLOW_FIRST'))
    slow_ms = int(os.environ.get('FAKE_SLOW_MS', '3000'))
    die_on_task = bool(os.environ.get('FAKE_DIE_ON_TASK'))
    die_on_task_id = os.environ.get('FAKE_DIE_ON_TASK_ID')
    die_on_subject = os.environ.get('FAKE_DIE_ON_SUBJECT')
    runlog = os.environ.get('FAKE_RUNLOG')
    die_after_result = bool(os.environ.get('FAKE_DIE_AFTER_RESULT'))
    silent = bool(os.environ.get('FAKE_SILENT'))
    silent_after_hb = bool(os.environ.get('FAKE_SILENT_AFTER_HEARTBEAT'))
    heartbeat = bool(os.environ.get('FAKE_HEARTBEAT'))
    # A2.4 boot-failure modes: die / hang BEFORE ever emitting READY (a pre-READY death →
    # crash-loop breaker / boot-deadline tests). ``FAKE_SPAWNLOG`` records EVERY process start
    # (one line per spawn) so a test can assert the breaker BOUNDED total spawns (no staging flood).
    exit_before_ready = bool(os.environ.get('FAKE_EXIT_BEFORE_READY'))
    never_ready = bool(os.environ.get('FAKE_NEVER_READY'))
    spawnlog = os.environ.get('FAKE_SPAWNLOG')

    if spawnlog:
        fd = os.open(spawnlog, os.O_CREAT | os.O_WRONLY | os.O_APPEND)
        os.write(fd, (str(os.getpid()) + '\n').encode())
        os.close(fd)

    if exit_before_ready:
        os._exit(1)  # deterministic boot failure: crash before signalling READY.
    if never_ready:
        while True:  # spawn, but never come up → the boot deadline must reap us.
            time.sleep(3600)

    # One-shot fault gate: only the FIRST worker to claim the marker faults; respawns run clean.
    once_file = os.environ.get('FAKE_FAULT_ONCE_FILE')
    if once_file and (die_on_task or silent or die_after_result or silent_after_hb):
        try:
            fd = os.open(once_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)  # we claimed the fault.
        except FileExistsError:
            # a prior worker already faulted; run normally.
            die_on_task = silent = die_after_result = silent_after_hb = False

    task_number = 0
    _emit('READY')
    for raw in sys.stdin:
        line = raw.strip()
        if not line.startswith('TASK '):
            continue
        task_number += 1
        task = json.loads(line[len('TASK ') :])
        task_id = task['id']

        if die_on_task and task_number == 1:
            os._exit(137)  # simulate SIGKILL-style death mid-task, no RESULT.

        if die_on_task_id is not None and task_id == die_on_task_id:
            os._exit(137)  # poison task: die whenever THIS id is received (no RESULT).

        if die_on_subject is not None and task_id.split('|', 1)[0] == die_on_subject:
            os._exit(137)  # poison subject: every game of this subject fails forever (no RESULT).

        if silent:
            while True:  # accept the task, then never make progress.
                time.sleep(3600)

        if silent_after_hb and task_number == 1:
            _emit(f'GAME {task_id} turn=1 ms=1')  # reader is now mid-stream ...
            while True:  # ... then never make progress → watchdog reaps us live.
                time.sleep(3600)

        if heartbeat:
            _emit(f'GAME {task_id} turn=1 ms=1')

        this_sleep = slow_ms if (slow_first and task_number == 1) else sleep_ms
        if this_sleep:
            time.sleep(this_sleep / 1000.0)

        # Report a realistic game length (not the scheduling-sleep): a bare ms=0 would trip the
        # production plausibility gate (bailout floor). ``this_sleep`` models wall-time for the
        # pool's scheduling tests; the game itself reports a plausible duration.
        game_ms = this_sleep if this_sleep >= 2000 else 60000
        if runlog:
            # Record the COMPLETED task_id (append-only, atomic per short line) so a test can
            # prove a committed game is never re-run across a crash/resume.
            fd = os.open(runlog, os.O_CREAT | os.O_WRONLY | os.O_APPEND)
            os.write(fd, (task_id + '\n').encode())
            os.close(fd)
        result = {'id': task_id, 'winner': 'a', 'kill_turn': 3, 'ms': game_ms, 'markers': [], 'log': None}
        _emit('RESULT ' + json.dumps(result))

        if die_after_result and task_number == 1:
            os._exit(137)  # died BETWEEN tasks: RESULT seen, but no follow-up READY.

        _emit('READY')


if __name__ == '__main__':
    main()
