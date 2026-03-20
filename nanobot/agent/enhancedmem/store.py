"""EnhancedMem store: EverMemOS-style memory backend."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.enhancedmem import boundary as boundary_mod
from nanobot.agent.enhancedmem.cluster import assign_memcell_to_cluster
from nanobot.agent.enhancedmem.life_profile import extract_and_apply_life_profile
from nanobot.agent.enhancedmem.memcell import (
    append_memcell,
    create_memcell,
    extract_episode,
    extract_eventlog,
    extract_foresight,
    format_conversation_for_extractors,
)
from nanobot.agent.enhancedmem.memory_md import MemoryMdManager
from nanobot.agent.enhancedmem.search import extract_episode_text, search
from nanobot.agent.enhancedmem.utils import history_path_for_date
from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session


class EnhancedMemStore:
    """EnhancedMem backend: MemCell, Episode, EventLog, Foresight, semantic clustering, Life Profile."""

    def __init__(self, workspace: Path, config: Any = None):
        self.workspace = Path(workspace)
        self.memory_dir = ensure_dir(self.workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.memcells_file = self.memory_dir / "memcells.jsonl"
        self.episodes_file = self.memory_dir / "episodes.jsonl"
        self.foresights_file = self.memory_dir / "foresights.jsonl"
        self.cluster_state_file = self.memory_dir / "cluster_state.json"
        self.config = config
        self._memory_md_max_chars = (
            getattr(config, "memory_md_max_chars", None) or 6000
        )
        self._life_profile_max_items = (
            getattr(config, "life_profile_max_items", None) or 80
        )
        self._memory_md = MemoryMdManager(
            self.memory_file,
            max_chars=self._memory_md_max_chars,
            config=config,
        )
        self._retrieve_method = getattr(config, "retrieve_method", "lightest")
        self._bm25_min_score_ratio = getattr(config, "bm25_min_score_ratio", 0.1)
        self._bm25_min_score_absolute = getattr(config, "bm25_min_score_absolute", 0.01)
        self._log_config(config)

    def _log_config(self, config: Any) -> None:
        """Log all resolved config at debug level."""
        resolved: dict[str, Any] = {
            "retrieve_method": self._retrieve_method,
            "memory_md_max_chars": self._memory_md_max_chars,
            "life_profile_max_items": self._life_profile_max_items,
        }
        if config is not None:
            if hasattr(config, "model_dump"):
                resolved.update(config.model_dump())
            else:
                for key in (
                    "retrieve_method",
                    "bm25_min_score_ratio",
                    "bm25_min_score_absolute",
                    "memory_md_max_chars",
                    "memory_consolidate_interval_messages",
                    "memory_consolidate_after_turn",
                    "life_profile_max_items",
                    "cluster_similarity_threshold",
                    "cluster_max_time_gap_days",
                ):
                    if hasattr(config, key):
                        resolved[key] = getattr(config, key)
        logger.debug("EnhancedMemStore config: {}", resolved)

    def read_long_term(self) -> str:
        return self._memory_md.read_long_term()

    def write_long_term(self, content: str) -> None:
        self._memory_md.write_long_term(content)

    def append_history(self, entry: str) -> None:
        """Append to HISTORY.YYMMDD.md for the date in entry, or today if no timestamp."""
        dt = datetime.now()
        if entry.strip().startswith("["):
            try:
                part = entry.strip()[1:10]  # [YYYY-MM-DD
                dt = datetime.strptime(part, "%Y-%m-%d")
            except ValueError:
                pass
        path = history_path_for_date(self.memory_dir, dt)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")
        logger.debug("EnhancedMem [file APPEND] {}: {} chars", path.name, len(entry))

    def get_memory_context(self, query: str | None = None) -> str:
        """Build memory context from MEMORY.md + retrieved episodes/events (by query or recent)."""
        long_term = self.read_long_term()
        parts = []

        if long_term:
            parts.append(f"## Long-term Memory\n{long_term}")

        episodes = self._retrieve_episodes(query=query, limit=5)
        ep_mode = "retrieved" if (query and query.strip()) else "recent"
        if episodes:
            ep_text = "\n\n".join(
                f"**{e.get('title', '')}** ({e.get('timestamp', '')[:10]}): {e.get('summary', '')}"
                for e in episodes
            )
            section = "## Retrieved Episodes" if ep_mode == "retrieved" else "## Recent Episodes"
            parts.append(f"{section}\n{ep_text}")

        history_hits = []
        if query and len(query.strip()) >= 2:
            history_hits = self._retrieve_history(query=query.strip(), limit=5)
            if history_hits:
                parts.append("## Relevant History\n" + "\n".join(history_hits))

        if parts:
            query_preview = (query or "")[:50] + ("..." if len(query or "") > 50 else "")
            logger.info(
                "EnhancedMem context loaded: MEMORY={}ch, episodes={} ({}){}, history={}",
                len(long_term), len(episodes), ep_mode, f', query="{query_preview}"' if query else "", len(history_hits),
            )

        if not parts:
            return ""
        return "\n\n".join(parts)

    def _retrieve_history(self, query: str, limit: int = 5) -> list[str]:
        """Retrieve EventLog lines matching query from HISTORY.YYMMDD.md via unified search."""
        documents: list[dict] = []
        for path in sorted(self.memory_dir.glob("HISTORY.*.md"), reverse=True)[:14]:
            try:
                text = path.read_text(encoding="utf-8")
                logger.debug("EnhancedMem [file READ] {}: {} lines (retrieve_history)", path.name, len(text.splitlines()))
                # sort_key: newer files first when score ties (invert YYMMDD)
                try:
                    yymmdd = int(path.stem.split(".")[-1])
                    inv = 999999 - yymmdd
                except (ValueError, IndexError):
                    inv = 0
                for idx, line in enumerate(text.splitlines()):
                    line = line.strip()
                    if line:
                        documents.append({
                            "line": line,
                            "path": path,
                            "sort_key": f"{inv:06d}_{idx:06d}",
                        })
            except OSError:
                continue
        if not documents:
            return []
        results = search(
            query=query,
            documents=documents,
            limit=limit,
            strategy=self._retrieve_method,
            text_extractor=lambda d: d["line"],
            sort_key_extractor=lambda d: d["sort_key"],
            bm25_min_score_ratio=self._bm25_min_score_ratio,
            bm25_min_score_absolute=self._bm25_min_score_absolute,
        )
        return [d["line"] for d, _ in results]

    def _retrieve_episodes(self, query: str | None = None, limit: int = 5) -> list[dict]:
        """Retrieve episodes by unified search with query, or recent N if no query."""
        if not self.episodes_file.exists():
            return []
        text = self.episodes_file.read_text(encoding="utf-8")
        lines = text.strip().splitlines()
        logger.debug("EnhancedMem [file READ] episodes.jsonl: {} lines (retrieve_episodes)", len(lines))
        episodes = []
        for line in lines:
            if not line.strip():
                continue
            try:
                episodes.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if not episodes:
            return []

        if not query or len(query.strip()) < 2:
            return list(reversed(episodes))[-limit:]

        results = search(
            query=query.strip(),
            documents=episodes,
            limit=limit,
            strategy=self._retrieve_method,
            text_extractor=extract_episode_text,
            sort_key_extractor=lambda ep: ep.get("timestamp", "") or "",
            bm25_min_score_ratio=self._bm25_min_score_ratio,
            bm25_min_score_absolute=self._bm25_min_score_absolute,
        )
        chosen = [ep for ep, _ in results]
        if not chosen:
            return list(reversed(episodes))[-limit:]
        return chosen

    def _get_recent_episodes(self, limit: int = 5) -> list[dict]:
        """Read last N episodes from episodes.jsonl."""
        if not self.episodes_file.exists():
            return []
        text = self.episodes_file.read_text(encoding="utf-8")
        lines = text.strip().splitlines()
        logger.debug("EnhancedMem [file READ] episodes.jsonl: {} lines (_get_recent_episodes)", len(lines))
        episodes = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                episodes.append(json.loads(line))
                if len(episodes) >= limit:
                    break
            except json.JSONDecodeError:
                continue
        return list(reversed(episodes))

    async def consolidate(
        self,
        session: "Session",
        provider: "LLMProvider",
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
        pending_user_message: object | None = None,
    ) -> bool:
        """Consolidate messages via boundary detection, MemCell, and extraction."""
        if archive_all:
            old_messages = session.messages
            keep_count = 0
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages or len(old_messages) < 2:
                return True

        try:
            if archive_all:
                should_end, should_wait, topic_summary = True, False, "会话归档"
            else:
                if pending_user_message is not None and hasattr(pending_user_message, "content"):
                    ts = getattr(pending_user_message, "timestamp", None) or datetime.now()
                    ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                    pending_dict = {
                        "role": "user",
                        "content": getattr(pending_user_message, "content", "") or "",
                        "timestamp": ts_str,
                    }
                    history_for_detect = old_messages
                    new_for_detect = [pending_dict]
                else:
                    history_for_detect = old_messages[:-1]
                    new_for_detect = old_messages[-1:]
                    if not history_for_detect:
                        history_for_detect = []
                        new_for_detect = old_messages
                should_end, should_wait, topic_summary = await boundary_mod.detect_boundary(
                    history_for_detect,
                    new_for_detect,
                    provider,
                    model,
                    memory_window=memory_window,
                )

            # If we've reached the configured short-term context size, we should
            # still consolidate even if the boundary detector returns should_end=false.
            # This keeps memory writes aligned with memory_window semantics.
            if (
                not archive_all
                and memory_window is not None
                and memory_window > 0
                and len(session.messages) >= memory_window
            ):
                should_end = True
                should_wait = False
                if not topic_summary:
                    topic_summary = "达到 memory_window 上限，强制切分"

            if not should_end and not archive_all:
                logger.debug("EnhancedMem boundary: should_end=false, skipping consolidate (wait for more or topic change)")
                return True

            memcell = create_memcell(old_messages, topic_summary)
            append_memcell(memcell, self.memcells_file)

            assign_memcell_to_cluster(
                memcell.get("event_id", ""),
                memcell.get("timestamp", ""),
                self.cluster_state_file,
            )

            await extract_episode(memcell, provider, model, self.episodes_file)
            await extract_eventlog(memcell, provider, model, self.append_history)
            await extract_foresight(memcell, provider, model, self.foresights_file)

            conv_text = format_conversation_for_extractors(memcell.get("original_data", []))
            await extract_and_apply_life_profile(
                self.workspace / "USER.md",
                memcell,
                conv_text,
                provider,
                model,
                max_items=self._life_profile_max_items,
            )

            ts = memcell.get("timestamp", datetime.now().isoformat())[:16]
            history_entry = f"[{ts}] {topic_summary}"
            self.append_history(history_entry)

            await self._memory_md.append_topic_summary(
                ts,
                topic_summary,
                provider,
                model,
                archive_all=archive_all,
            )

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("EnhancedMem consolidate done: last_consolidated={}", session.last_consolidated)
            return True
        except Exception:
            logger.exception("EnhancedMem consolidate failed")
            return False
