"""Concrete :class:`~pipeline.sim.engine.SimEngine` backends.

Importing this package registers every shipped engine into the module-level
registry (:func:`~pipeline.sim.engine.register_engine`) as an import side effect,
so :func:`~pipeline.sim.engine.get_engine` resolves them after the sim package is
imported: :class:`~pipeline.sim.engines.forge.ForgeEngine` (Forge sim-AI) and
:class:`~pipeline.sim.engines.xmage.XMageEngine` (XMage ComputerPlayer7).
"""

from __future__ import annotations

from pipeline.sim.engines.forge import ForgeEngine
from pipeline.sim.engines.xmage import XMageEngine

__all__ = ('ForgeEngine', 'XMageEngine')
