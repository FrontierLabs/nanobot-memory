"""Memory backend factory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.agent.memory import MemoryStore

if TYPE_CHECKING:
    from nanobot.agent.memory_base import MemoryBackend
    from nanobot.config.schema import AgentDefaults, Config, MemoryConfig


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
