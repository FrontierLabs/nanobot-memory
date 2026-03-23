"""Standalone EnhancedMem runner for offline ingestion and context building.

This module lets you:
- Ingest a full conversation into EnhancedMem without going through AgentLoop
- Optionally run consolidation incrementally (simulating online behavior)
- When ``RunnerMessage.session_key`` changes (e.g. LoCoMo ``session_0`` → ``session_1``),
  archive the unconsolidated tail with ``archive_all=True`` and clear the session, matching
  interactive ``/new`` semantics
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
    # Dataset session id (e.g. LoCoMo ``session_0``). When it changes between
    # messages, runner mirrors ``/new``: archive unconsolidated tail then clear.
    session_key: str | None = None


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
        memory_consolidate_after_turn: int = 0,
        config: Any | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.provider = provider
        self.model = model
        self.memory_window = memory_window

        # Match AgentLoop's consolidation threshold semantics
        if memory_consolidate_interval is not None:
            self._consolidate_threshold = memory_consolidate_interval
        else:
            # `memory_consolidate_after_turn` is an integer:
            # - 0 (or unset) => disable "early boundary detection" => use `memory_window`
            # - N > 0      => start running boundary detection once we have >= N turns/messages
            #                (implemented as a consolidation trigger threshold)
            #
            # Legacy support: if callers still pass `true`, coerce to the historical
            # enhancedmem runner value (~10).
            if isinstance(memory_consolidate_after_turn, bool):
                after_turn_n = 10 if memory_consolidate_after_turn else 0
            else:
                after_turn_n = int(memory_consolidate_after_turn or 0)

            self._consolidate_threshold = (
                min(after_turn_n, memory_window) if after_turn_n > 0 else memory_window
            )

        self._store = EnhancedMemStore(self.workspace, config=config)
        self._session = Session(key="enhancedmem:eval")

    @property
    def store(self) -> EnhancedMemStore:
        return self._store

    @property
    def session(self) -> Session:
        return self._session

    def _load_profile_sections(self) -> str:
        """Load optional profile bootstrap files for retrieval context."""
        parts: list[str] = []
        for filename in ("USER.md", "IDENTITY.md"):
            file_path = self.workspace / filename
            if not file_path.exists():
                continue
            try:
                content = file_path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if content:
                parts.append(f"## {filename}\n\n{content}")
        return "\n\n".join(parts)

    async def _archive_unconsolidated_then_clear(self) -> None:
        """Match AgentLoop ``/new``: full-archive unconsolidated tail, then clear session."""
        snapshot = self._session.messages[self._session.last_consolidated :]
        if snapshot:
            temp = Session(key=self._session.key)
            temp.messages = list(snapshot)
            await self._store.consolidate(
                temp,
                self.provider,
                self.model,
                archive_all=True,
                memory_window=self.memory_window,
                pending_user_message=None,
            )
        self._session.clear()

    async def ingest(self, messages: Sequence[RunnerMessage] | Iterable[RunnerMessage]) -> None:
        """Ingest a sequence of messages and run consolidation when threshold reached."""
        prev_session_key: str | None = None
        for msg in messages:
            sk = msg.session_key
            if (
                sk is not None
                and prev_session_key is not None
                and sk != prev_session_key
            ):
                await self._archive_unconsolidated_then_clear()

            ts = msg.timestamp or datetime.now()
            msg_dict = {
                "role": msg.role,
                "content": msg.content,
                "timestamp": ts.isoformat(),
            }
            self._session.messages.append(msg_dict)
            if sk is not None:
                prev_session_key = sk

            unconsolidated = len(self._session.messages) - self._session.last_consolidated
            if unconsolidated >= self._consolidate_threshold:
                logger.debug(
                    "EnhancedMemRunner ingest: unconsolidated={} >= threshold={}, running consolidate() with last_consolidated={}",
                    unconsolidated,
                    self._consolidate_threshold,
                    self.session.last_consolidated
                )
                await self._store.consolidate(
                    self._session,
                    self.provider,
                    self.model,
                    archive_all=False,
                    memory_window=self.memory_window,
                    pending_user_message=(msg if msg.role == "user" else None),
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

    def get_memory_context(
        self, query: str | None = None, use_profiles: bool = False
    ) -> str:
        """Return composed memory context; optionally include USER/IDENTITY profiles."""
        memory_context = self._store.get_memory_context(query=query)
        if not use_profiles:
            return memory_context

        profile_context = self._load_profile_sections()
        if memory_context and profile_context:
            return f"{profile_context}\n\n{memory_context}"
        if profile_context:
            return profile_context
        return memory_context

