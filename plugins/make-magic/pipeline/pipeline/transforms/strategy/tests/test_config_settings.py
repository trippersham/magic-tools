"""StrategyConfig (pydantic-settings) contract tests.

The config was migrated from a frozen dataclass to a ``pydantic-settings``
:class:`BaseSettings` model. These pin the three properties the engine relies on:

* **hashable + frozen** — it is the ``@cache`` key for ``load_assets`` /
  ``build_substantiveness``; if it stops being hashable those break.
* **``@cache`` memoization intact** — equal configs return the SAME cached asset
  instance (object identity), proving the hash/eq contract still memoizes.
* **env-override is opt-in + namespaced + byte-parity by default** — a
  ``MAKEMAGIC_STRATEGY_``-prefixed var changes the field; with NO such env set the
  default construction reproduces the validated engine exactly.
"""

from __future__ import annotations

import pytest

from pipeline.transforms.strategy.assets import load_assets
from pipeline.transforms.strategy.config import DEFAULT_CONFIG, StrategyConfig
from pipeline.transforms.strategy.determine_strategy_substantiveness import build_substantiveness

#: The validated engine's default field values (the byte-parity contract).
_EXPECTED_DEFAULTS = {
    'gy_distinct': 4,
    'gy_ratio': 0.15,
    'conf_high': 0.66,
    'conf_low': 0.42,
    'identity_ratio': 0.5,
    'max_identity': 3,
    'typal_dom_share': 0.70,
    'over_t': 0.5,
    'ctrl_min_cs': 3,
    'combo_conf': frozenset({'LOW'}),
    'combo_win_min': 2,
    'combo_primary_share': 0.50,
    'combo_primary_margin': 0.30,
    'cmd_weight': 5.0,
}


@pytest.fixture(autouse=True)
def _clear_strategy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub any ambient MAKEMAGIC_STRATEGY_* so a case starts from the defaults."""
    import os

    for k in list(os.environ):
        if k.startswith('MAKEMAGIC_STRATEGY_'):
            monkeypatch.delenv(k, raising=False)


def test_config_is_hashable_and_frozen() -> None:
    """Hashable (the @cache key) and immutable (frozen)."""
    assert hash(StrategyConfig()) == hash(DEFAULT_CONFIG)
    assert StrategyConfig() == DEFAULT_CONFIG
    with pytest.raises((TypeError, ValueError)):  # pydantic raises on frozen set
        DEFAULT_CONFIG.max_identity = 99  # type: ignore[misc]


def test_cache_memoization_intact() -> None:
    """Equal configs return the SAME cached instance — hash/eq still memoize @cache."""
    assert load_assets(StrategyConfig()) is load_assets(StrategyConfig())
    assert build_substantiveness(StrategyConfig()) is build_substantiveness(StrategyConfig())


def test_default_no_env_is_byte_parity() -> None:
    """With NO MAKEMAGIC_STRATEGY_* env set, every field equals the validated value."""
    cfg = StrategyConfig()
    for field, expected in _EXPECTED_DEFAULTS.items():
        assert getattr(cfg, field) == expected, f'{field}: {getattr(cfg, field)!r} != {expected!r}'
    # frozenset fields stay frozensets (hashable)
    assert isinstance(cfg.combo_conf, frozenset)


def test_env_override_is_opt_in_and_namespaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A namespaced env var overrides its field; construction without it does not."""
    monkeypatch.setenv('MAKEMAGIC_STRATEGY_MAX_IDENTITY', '2')
    cfg = StrategyConfig()
    assert cfg.max_identity == 2
    # unrelated fields untouched (no env set for them)
    assert cfg.cmd_weight == 5.0


def test_with_overrides_returns_frozen_copy() -> None:
    """with_overrides is a model_copy: a new frozen instance, original untouched."""
    c2 = DEFAULT_CONFIG.with_overrides(max_identity=2)
    assert c2.max_identity == 2
    assert DEFAULT_CONFIG.max_identity == 3
    assert hash(c2)  # still hashable
