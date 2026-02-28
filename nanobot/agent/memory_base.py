"""Memory backend protocol for persistent agent memory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session


class MemoryBackend(Protocol):
    """Protocol for memory backends. Both default and EnhancedMem implement this."""

    def read_long_term(self) -> str:
        """Read long-term memory content."""
        ...

    def write_long_term(self, content: str) -> None:
        """Write long-term memory content."""
        ...

    def append_history(self, entry: str) -> None:
        """Append an entry to history log."""
        ...

    def get_memory_context(self, query: str | None = None) -> str:
        """Get memory context string for system prompt. query=current message for retrieval."""
        ...

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
        pending_user_message: object | None = None,
    ) -> bool:
        """Consolidate messages into long-term memory. Returns True on success."""
        ...
