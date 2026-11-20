"""Tests for the repo-root stacked-PR merge helper `scripts/merge-stacked`.

A native stacked PR can only be merged via the async endpoint, and "enqueued" is
not "merged" (a ruleset-blocked merge stays pending while the PR stays OPEN). We
cannot drive a live stacked PR here — that is the Phase 5 dogfood — so this proves
the two load-bearing pieces offline:

  * `parse_uuid` / `classify_state`: the script factors these as sourceable bash
    functions; we exercise them directly against captured merge-async JSON.
  * the poll loop: we run the whole script with a stubbed `gh` on PATH and assert
    it exits 0 ONLY when the PR actually reaches MERGED, and fails loud (non-zero)
    when the enqueue never lands within the budget, on a draft, and on a closed PR.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# repo root = .../plugins/make-magic/pipeline/tests/this-file -> parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / 'scripts' / 'merge-stacked'
_FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'merge_stacked'


# --- unit: parse_uuid / classify_state via `source` ---------------------------


def _source_and_call(snippet: str, *, stdin: str = '') -> subprocess.CompletedProcess[str]:
    """Source the script (the guard keeps `main` from running) and call a helper."""
    return subprocess.run(
        ['bash', '-c', f'source "$1"; {snippet}', 'bash', str(_SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
    )


def test_parse_uuid_from_nested_details() -> None:
    payload = (_FIXTURES / 'enqueue_pending.json').read_text(encoding='utf-8')
    result = _source_and_call('parse_uuid', stdin=payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'b3f8c1d2-7e4a-4c9b-9a21-0f6e5d4c3b2a'


def test_parse_uuid_from_top_level() -> None:
    payload = (_FIXTURES / 'enqueue_toplevel_uuid.json').read_text(encoding='utf-8')
    result = _source_and_call('parse_uuid', stdin=payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '11111111-2222-3333-4444-555555555555'


def test_parse_uuid_fails_loud_when_absent() -> None:
    # An error/malformed response carries no uuid; the helper must exit non-zero
    # so the caller can never poll a bogus uuid or mistake it for an enqueue.
    result = _source_and_call('parse_uuid', stdin='{"message":"Not Found"}')
    assert result.returncode != 0
    assert 'uuid' in result.stderr.lower()


def test_parse_uuid_fails_loud_on_non_json() -> None:
    result = _source_and_call('parse_uuid', stdin='403 Forbidden')
    assert result.returncode != 0


def test_classify_state_matrix() -> None:
    cases = {
        ('MERGED', 'pending'): 'merged',  # PR state is the authoritative landed signal
        ('MERGED', 'merged'): 'merged',
        ('OPEN', 'pending'): 'pending',  # enqueued but not landed -> keep polling
        ('OPEN', 'conflict'): 'failed',
        ('OPEN', 'error'): 'failed',
        ('OPEN', 'failure'): 'failed',
        ('CLOSED', 'pending'): 'failed',  # closed-unmerged is a hard failure
    }
    for (pr_state, async_status), want in cases.items():
        result = _source_and_call(f'classify_state {pr_state} {async_status}')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == want, f'{pr_state}/{async_status}'


# --- end-to-end: the poll loop against a stubbed `gh` -------------------------


def _write_fake_gh(bin_dir: Path) -> None:
    """A stub `gh` whose behavior is driven by FAKE_* env vars the test sets.

    It answers the exact calls merge-stacked makes: repo slug, the pre-check and
    loop `pr view`, the `merge-async` PUT (enqueue), and the `merge-async/{uuid}`
    GET (poll). A counter file makes the loop `pr view` flip to MERGED after
    FAKE_MERGE_AFTER polls, or never (to prove the fail-loud timeout).
    """
    gh = bin_dir / 'gh'
    gh.write_text(
        r"""#!/usr/bin/env bash
set -euo pipefail
args="$*"
case "$1" in
  repo)
    echo "owner/repo"
    ;;
  pr)
    # pr view ...
    if [[ "$args" == *"state,isDraft"* ]]; then
      printf '{"state":"%s","isDraft":%s}\n' "${FAKE_STATE:-OPEN}" "${FAKE_IS_DRAFT:-false}"
    else
      # loop poll of pr state (uses --jq .state); count it
      n=0
      [ -f "$FAKE_COUNTER" ] && n=$(cat "$FAKE_COUNTER")
      n=$((n + 1)); echo "$n" > "$FAKE_COUNTER"
      if [ -n "${FAKE_MERGE_AFTER:-}" ] && [ "$n" -ge "$FAKE_MERGE_AFTER" ]; then
        echo "MERGED"
      else
        echo "${FAKE_STATE:-OPEN}"
      fi
    fi
    ;;
  api)
    if [[ "$args" == *"-X PUT"* ]]; then
      cat "$FAKE_ENQUEUE_FILE"
    else
      cat "$FAKE_POLL_FILE"
    fi
    ;;
  *)
    echo "fake gh: unhandled: $args" >&2; exit 99
    ;;
esac
""",
        encoding='utf-8',
    )
    gh.chmod(0o755)


def _run_script(tmp_path: Path, env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    _write_fake_gh(bin_dir)
    env = os.environ.copy()
    env['PATH'] = f'{bin_dir}{os.pathsep}{env["PATH"]}'
    env['FAKE_COUNTER'] = str(tmp_path / 'counter')
    env['FAKE_ENQUEUE_FILE'] = str(_FIXTURES / 'enqueue_pending.json')
    env['FAKE_POLL_FILE'] = str(_FIXTURES / 'poll_pending.json')
    # Zero interval + tiny budget keep the test instant.
    env['MERGE_STACKED_POLL_INTERVAL'] = '0'
    env['MERGE_STACKED_MAX_ATTEMPTS'] = '5'
    env.update(env_overrides)
    return subprocess.run(
        ['bash', str(_SCRIPT), '123'],
        capture_output=True,
        text=True,
        env=env,
    )


def test_poll_exits_zero_when_pr_reaches_merged(tmp_path: Path) -> None:
    result = _run_script(tmp_path, {'FAKE_STATE': 'OPEN', 'FAKE_MERGE_AFTER': '2'})
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'MERGED' in result.stdout
    # The captured uuid must have been parsed and used in the poll.
    assert 'enqueued, uuid=b3f8c1d2' in result.stdout


def test_poll_fails_loud_when_merge_never_lands(tmp_path: Path) -> None:
    # The enqueue returns pending and the PR stays OPEN forever (the ci-ok-blocked
    # case). The script must exhaust its budget and exit NON-ZERO — never exit 0.
    result = _run_script(tmp_path, {'FAKE_STATE': 'OPEN'})  # no FAKE_MERGE_AFTER
    assert result.returncode != 0
    assert 'did not merge' in result.stderr
    assert 'ci-ok' in result.stderr


def test_refuses_a_draft(tmp_path: Path) -> None:
    result = _run_script(tmp_path, {'FAKE_STATE': 'OPEN', 'FAKE_IS_DRAFT': 'true'})
    assert result.returncode == 1
    assert 'draft' in result.stderr.lower()


def test_refuses_a_closed_pr(tmp_path: Path) -> None:
    result = _run_script(tmp_path, {'FAKE_STATE': 'CLOSED'})
    assert result.returncode == 1
    assert 'CLOSED' in result.stderr


def test_already_merged_is_a_noop(tmp_path: Path) -> None:
    result = _run_script(tmp_path, {'FAKE_STATE': 'MERGED'})
    assert result.returncode == 0
    assert 'already MERGED' in result.stdout


def test_rejects_non_numeric_pr_argument(tmp_path: Path) -> None:
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    _write_fake_gh(bin_dir)
    env = os.environ.copy()
    env['PATH'] = f'{bin_dir}{os.pathsep}{env["PATH"]}'
    result = subprocess.run(
        ['bash', str(_SCRIPT), 'not-a-number'],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    assert 'positive integer' in result.stderr


def test_fixtures_are_valid_json_with_a_uuid() -> None:
    """Guards the captured fixtures themselves against bitrot."""
    for name in ('enqueue_pending.json', 'enqueue_toplevel_uuid.json', 'poll_pending.json'):
        doc = json.loads((_FIXTURES / name).read_text(encoding='utf-8'))
        assert doc.get('status') == 'pending'
    nested = json.loads((_FIXTURES / 'enqueue_pending.json').read_text(encoding='utf-8'))
    assert nested['details']['uuid']
