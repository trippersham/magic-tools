"""Collection-layer error types.

`CollectionError` is the ONE exception the CLI wrapper (`run.main`) treats as a
clean, user-facing failure (alongside `FileNotFoundError` and the Airtable
`ReadOnlyStoreError` / `AirtableConfigError`). It subclasses `RuntimeError` (the
same pattern the sibling `ReadOnlyStoreError` follows), so a raw `KeyError` /
`ValueError` / `pydantic.ValidationError` from a genuine defect is NOT swallowed —
it tracebacks.

Deliberate, user-facing raises (unknown ``--field``, missing creds, malformed
hand-edited YAML, unresolved link names, bad user JSON) raise `CollectionError`;
programmer errors stay as their native type so bugs surface loudly.
"""

from __future__ import annotations

__all__ = ('CollectionError',)


class CollectionError(RuntimeError):
    """A clean, user-facing collection failure (bad input / config / state).

    Lets `run.main` catch EXACTLY the user-facing failures and let real defects
    (`KeyError`, `ValueError`, `ValidationError`) traceback.
    """

    message: str

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
