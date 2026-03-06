"""Standalone EnhancedMem runner for offline ingestion and context building.

This module lets you:
- Ingest a full conversation into EnhancedMem without going through AgentLoop
- Optionally run consolidation incrementally (simulating online behavior)
- Finalize at the end so the tail segment is always written to memory
- Read back the composed memory context via EnhancedMemStore.get_memory_context()
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from loguru import logger

from nanobot.agent.enhancedmem.store import EnhancedMemStore
from nanobot.session.manager import Session


@dataclass
class RunnerMessage:
    """Lightweight message format for EnhancedMemRunner."""

    role: str
    content: str
    timestamp: datetime | None = None


class EnhancedMemRunner:
    """Offline runner for EnhancedMem.

    Typical usage:
        runner = EnhancedMemRunner(workspace, provider, model)
        await runner.ingest(messages)
        await runner.finalize()
        ctx = runner.get_memory_context(query)
    """

    def __init__(
        self,
        workspace: Path | str,
        provider: Any,
        model: str,
        *,
        memory_window: int = 100,
        memory_consolidate_interval: int | None = None,
        memory_consolidate_after_turn: bool = False,
        config: Any | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.provider = provider
        self.model = model
        self.memory_window = memory_window

        # Match AgentLoop's consolidation threshold semantics
        if memory_consolidate_interval is not None:
            self._consolidate_threshold = memory_consolidate_interval
        elif memory_consolidate_after_turn:
            self._consolidate_threshold = min(6, memory_window)
        else:
            self._consolidate_threshold = memory_window

        self._store = EnhancedMemStore(self.workspace, config=config)
        self._session = Session(key="enhancedmem:eval")

    @property
    def store(self) -> EnhancedMemStore:
        return self._store

    @property
    def session(self) -> Session:
        return self._session

    async def ingest(self, messages: Sequence[RunnerMessage] | Iterable[RunnerMessage]) -> None:
        """Ingest a sequence of messages and run consolidation when threshold reached."""
        for msg in messages:
            ts = msg.timestamp or datetime.now()
            msg_dict = {
                "role": msg.role,
                "content": msg.content,
                "timestamp": ts.isoformat(),
            }
            self._session.messages.append(msg_dict)

            unconsolidated = len(self._session.messages) - self._session.last_consolidated
            if unconsolidated >= self._consolidate_threshold:
                logger.debug(
                    "EnhancedMemRunner ingest: unconsolidated={} >= threshold={}, running consolidate()",
                    unconsolidated,
                    self._consolidate_threshold,
                )
                await self._store.consolidate(
                    self._session,
                    self.provider,
                    self.model,
                    archive_all=False,
                    memory_window=self.memory_window,
                    pending_user_message=None,
                )

    async def finalize(self) -> None:
        """Finalize ingestion by forcing remaining tail messages into memory."""
        remaining = len(self._session.messages) - self._session.last_consolidated
        if remaining <= 0:
            return

        logger.info(
            "EnhancedMemRunner finalize: consolidating remaining {} messages (last_consolidated={})",
            remaining,
            self._session.last_consolidated,
        )
        # Use archive_all=True so the store processes all remaining messages.
        # Otherwise consolidate() returns early when len(messages) <= keep_count (memory_window//2),
        # so short conversations (e.g. smoke test with 10 messages) would write nothing.
        await self._store.consolidate(
            self._session,
            self.provider,
            self.model,
            archive_all=True,
            memory_window=self.memory_window,
            pending_user_message=None,
        )

    def get_memory_context(self, query: str | None = None) -> str:
        """Return the composed memory context for a query."""
        return self._store.get_memory_context(query=query)

