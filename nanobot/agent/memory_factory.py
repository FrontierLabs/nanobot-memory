"""Memory backend factory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.agent.memory import MemoryStore
from nanobot.config.schema import AgentDefaults

if TYPE_CHECKING:
    from nanobot.agent.memory_base import MemoryBackend
    from nanobot.config.schema import Config, MemoryConfig


def make_memory_store(
    workspace: Path,
    config: MemoryConfig | AgentDefaults | Config,
) -> MemoryBackend:
    """Create a memory backend from config."""
    if hasattr(config, "memory"):
        mem_config = config.memory
    else:
        mem_config = config

    backend = getattr(mem_config, "backend", "default")
    if backend == "enhancedmem":
        from nanobot.agent.enhancedmem.store import EnhancedMemStore

        enhanced_config = getattr(mem_config, "enhancedmem", None)
        return EnhancedMemStore(workspace, enhanced_config)
    return MemoryStore(workspace)


def resolve_memory_for_agent_loop(
    workspace: Path,
    agent_defaults: AgentDefaults,
) -> tuple[MemoryBackend, int | None, bool]:
    """Build `AgentLoop` memory args in one place (backend + EnhancedMem consolidate knobs).

    Keeps CLI/gateway/cron paths aligned without duplicating `if enhancedmem` blocks.
    Backend selection stays in the `MemoryBackend` protocol + `make_memory_store`;
    full OpenClaw-style agent hooks are optional future work if you need observers
    outside the memory class.
    """
    store = make_memory_store(workspace, agent_defaults)
    mem = agent_defaults.memory
    if mem.backend != "enhancedmem":
        return store, None, False
    c = mem.enhancedmem
    return (
        store,
        c.memory_consolidate_interval_messages,
        c.memory_consolidate_after_turn,
    )
