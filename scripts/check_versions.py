#!/usr/bin/env python3
"""Assert the plugin version is in lockstep across its two declaration sites.

The plugin's one version lives in two files that ship together: the marketplace
manifest `plugins/make-magic/.claude-plugin/plugin.json` (`$.version`) and the
Python package `plugins/make-magic/pipeline/pyproject.toml` (`[project].version`).
A bump that updates one but not the other is drift that ships a wrong marketplace
version. This linter fails the build (exit 1, naming both values) when they differ.

It is wired into `scripts/check` ahead of the context linter so it runs inside
`ci-ok` at PR time, in BOTH `--fast` and full modes — the assertion is a cheap
file read, so there is no reason to gate it behind the slow pytest run. The same
comparison is re-asserted at release time by `.github/workflows/release.yml`
against the pushed tag, which closes the gap between the merged version and the
version the tag claims.
"""

import argparse
import json
import sys
import tomllib
from pathlib import Path

_PLUGIN_JSON = Path('plugins/make-magic/.claude-plugin/plugin.json')
_PYPROJECT = Path('plugins/make-magic/pipeline/pyproject.toml')


def check(root):
    """Yield one finding if the two version files disagree; nothing when matched."""
    root = root.resolve()
    plugin_version = json.loads((root / _PLUGIN_JSON).read_text(encoding='utf-8'))['version']
    pyproject_version = tomllib.loads((root / _PYPROJECT).read_text(encoding='utf-8'))['project']['version']
    if plugin_version != pyproject_version:
        yield (
            f'version lockstep mismatch: {_PLUGIN_JSON} = {plugin_version!r} != '
            f'{_PYPROJECT} = {pyproject_version!r} — bump both to the same version.'
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        findings = list(check(args.root))
    except (OSError, UnicodeError, ValueError, KeyError) as error:
        findings = [str(error)]
    if findings:
        for finding in findings:
            print(f'versions: FAIL: {finding}', file=sys.stderr)
        return 1
    print('versions: PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
