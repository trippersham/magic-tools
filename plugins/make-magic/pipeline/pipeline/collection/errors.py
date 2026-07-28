"""Collection-layer error types.

`CollectionError` is the ONE exception the CLI wrapper (`run.main`) treats as a
clean, user-facing failure (alongside `FileNotFoundError` and the Airtable
`ReadOnlyStoreError` / `AirtableConfigError`). It subclasses `ValueError` so the
existing ``pytest.raises(ValueError)`` tests (and any caller catching
`ValueError`) keep passing, while a raw `KeyError` / `RuntimeError` /
`pydantic.ValidationError` from a genuine defect is NOT swallowed — it tracebacks.

Deliberate, user-facing raises (unknown ``--field``, missing creds, malformed
hand-edited YAML, unresolved link names, bad user JSON) raise `CollectionError`;
programmer errors stay as their native type so bugs surface loudly.
"""

from __future__ import annotations

__all__ = ('CollectionError',)


class CollectionError(ValueError):
    """A clean, user-facing collection failure (bad input / config / state).

    Subclasses `ValueError` so existing ``pytest.raises(ValueError)`` assertions
    remain valid, while letting `run.main` catch EXACTLY the user-facing failures
    and let real defects (`KeyError`, `RuntimeError`, `ValidationError`) traceback.
    """
