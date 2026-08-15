"""Concrete :class:`~pipeline.sim.engine.SimEngine` backends.

Importing this package registers every shipped engine into the module-level
registry (:func:`~pipeline.sim.engine.register_engine`) as an import side effect,
so :func:`~pipeline.sim.engine.get_engine` resolves them after the sim package is
imported. Today the only backend is :class:`~pipeline.sim.engines.forge.ForgeEngine`
(the stock-heuristic Forge path); XMage lands in a later task.
"""

from __future__ import annotations

from pipeline.sim.engines.forge import ForgeEngine

__all__ = ('ForgeEngine',)
