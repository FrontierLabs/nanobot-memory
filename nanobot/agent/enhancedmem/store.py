"""EnhancedMem store: EverMemOS-style memory backend."""

from __future__ import annotations

import json
import re
import uuid

import json_repair
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.enhancedmem.cluster import assign_memcell_to_cluster
from nanobot.agent.enhancedmem.prompts_zh import (
    CONV_BOUNDARY_DETECTION_PROMPT,
    DEFAULT_CUSTOM_INSTRUCTIONS,
    EVENT_LOG_PROMPT,
    FORESIGHT_GENERATION_PROMPT,
    GROUP_EPISODE_GENERATION_PROMPT,
    PROFILE_LIFE_UPDATE_PROMPT,
)
from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session

# Force-split limits (from EverMemOS)
HARD_TOKEN_LIMIT = 8192
HARD_MESSAGE_LIMIT = 50

# Approximate tokens from chars (for languages without tiktoken)
CHARS_PER_TOKEN = 3


def _estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def _extract_json_object(text: str) -> str | None:
    """Extract outermost {...} from text, handling nested braces and strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    quote_char = ""
    for i, c in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if in_string:
            if c == "\\":
                escape = True
            elif c == quote_char:
                in_string = False
            continue
        if c in ('"', "'"):
            in_string = True
            quote_char = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _history_path_for_date(memory_dir: Path, dt: datetime) -> Path:
    """Get HISTORY.YYMMDD.md path for a date."""
    return memory_dir / f"HISTORY.{dt.strftime('%y%m%d')}.md"


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

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        """Append to HISTORY.YYMMDD.md for the date in entry, or today if no timestamp."""
        dt = datetime.now()
        if entry.strip().startswith("["):
            try:
                part = entry.strip()[1:10]  # [YYYY-MM-DD
                dt = datetime.strptime(part, "%Y-%m-%d")
            except ValueError:
                pass
        path = _history_path_for_date(self.memory_dir, dt)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        """Build memory context from MEMORY.md + recent episodes."""
        long_term = self.read_long_term()
        parts = []

        if long_term:
            parts.append(f"## Long-term Memory\n{long_term}")

        recent = self._get_recent_episodes(limit=3)
        if recent:
            ep_text = "\n\n".join(
                f"**{e.get('title', '')}** ({e.get('timestamp', '')[:10]}): {e.get('summary', '')}"
                for e in recent
            )
            parts.append(f"## Recent Episodes\n{ep_text}")

        if not parts:
            return ""
        return "\n\n".join(parts)

    def _get_recent_episodes(self, limit: int = 5) -> list[dict]:
        """Read last N episodes from episodes.jsonl."""
        if not self.episodes_file.exists():
            return []
        lines = self.episodes_file.read_text(encoding="utf-8").strip().splitlines()
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

    def _format_messages_for_prompt(self, messages: list[dict]) -> str:
        """Format Nanobot session messages for boundary detection."""
        lines = []
        for m in messages:
            content = m.get("content")
            if not content:
                continue
            role = m.get("role", "unknown").upper()
            ts = m.get("timestamp", "")[:16] if m.get("timestamp") else ""
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{ts}] {role}{tools}: {content}")
        return "\n".join(lines)

    def _time_gap_info(self, history: list[dict], new_msgs: list[dict]) -> str:
        """Calculate time gap between last history msg and first new msg."""
        if not history or not new_msgs:
            return "无时间间隔信息"
        last_ts = history[-1].get("timestamp") or ""
        first_ts = new_msgs[0].get("timestamp") or ""
        if not last_ts or not first_ts:
            return "无时间戳"
        try:
            last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            first_dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            diff = (first_dt - last_dt).total_seconds()
            if diff < 60:
                return f"间隔 {int(diff)} 秒（即时回复）"
            if diff < 3600:
                return f"间隔 {int(diff // 60)} 分钟"
            if diff < 86400:
                return f"间隔 {int(diff // 3600)} 小时"
            return f"间隔 {int(diff // 86400)} 天（可能为新对话）"
        except (ValueError, TypeError):
            return "时间解析失败"

    def _estimate_total_tokens(self, messages: list[dict]) -> int:
        total = 0
        for m in messages:
            content = m.get("content") or ""
            ts = str(m.get("timestamp", ""))
            total += _estimate_tokens(str(content) + ts)
        return total

    async def _detect_boundary(
        self,
        history_msgs: list[dict],
        new_msgs: list[dict],
        provider: "LLMProvider",
        model: str,
    ) -> tuple[bool, bool, str]:
        """Run LLM boundary detection. Returns (should_end, should_wait, topic_summary)."""
        if not new_msgs:
            return False, True, ""

        total_tokens = self._estimate_total_tokens(history_msgs + new_msgs)
        total_messages = len(history_msgs) + len(new_msgs)

        if total_tokens >= HARD_TOKEN_LIMIT or total_messages >= HARD_MESSAGE_LIMIT:
            if len(history_msgs) >= 2:
                return True, False, "达到消息/Token 上限，强制切分"

        conv_history = self._format_messages_for_prompt(history_msgs)
        new_text = self._format_messages_for_prompt(new_msgs)
        time_gap = self._time_gap_info(history_msgs, new_msgs)

        if not history_msgs:
            return False, False, ""

        logger.debug("EnhancedMem boundary detection: {} history + {} new messages", len(history_msgs), len(new_msgs))
        prompt = CONV_BOUNDARY_DETECTION_PROMPT.format(
            conversation_history=conv_history or "(无)",
            time_gap_info=time_gap,
            new_messages=new_text,
        )

        for attempt in range(3):
            try:
                response = await provider.chat(
                    messages=[
                        {"role": "system", "content": "你是一名对话情节边界分析专家。仅返回JSON，不要其他说明。"},
                        {"role": "user", "content": prompt},
                    ],
                    tools=None,
                    model=model,
                    temperature=0.1,
                )
                text = (response.content or "").strip()
                json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group())
                    should_end = bool(data.get("should_end", False))
                    should_wait = bool(data.get("should_wait", True))
                    topic_summary = str(data.get("topic_summary", "") or "")
                    if should_end:
                        should_wait = False
                    return should_end, should_wait, topic_summary
            except Exception as e:
                logger.warning("Boundary detection attempt {} failed: {}", attempt + 1, e)

        return False, True, ""

    def _create_memcell(self, messages: list[dict], summary: str) -> dict:
        """Create MemCell dict from messages."""
        last_ts = messages[-1].get("timestamp", datetime.now().isoformat()) if messages else datetime.now().isoformat()
        try:
            dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            dt = datetime.now()

        return {
            "event_id": str(uuid.uuid4()),
            "original_data": [
                {
                    "role": m.get("role", "user"),
                    "content": m.get("content", ""),
                    "timestamp": m.get("timestamp", ""),
                    "speaker_name": m.get("role", "user").upper(),
                }
                for m in messages if m.get("content")
            ],
            "timestamp": dt.isoformat(),
            "summary": summary or "对话片段",
            "participants": list({m.get("role", "user") for m in messages}),
            "type": "conversation",
        }

    def _compact_memory_text(self, content: str) -> str:
        """Truncate memory to stay under max chars, keeping most recent facts."""
        if len(content) <= self._memory_md_max_chars:
            return content
        lines = content.strip().splitlines()
        header = []
        facts = []
        for line in lines:
            if line.strip().startswith("- "):
                facts.append(line)
            elif not facts:
                header.append(line)
            else:
                facts.append(line)
        keep = self._memory_md_max_chars // 80
        kept_facts = facts[-keep:] if len(facts) > keep else facts
        result = "\n".join(header + [""] + kept_facts) + "\n"
        return result[: self._memory_md_max_chars]

    def _append_memcell(self, memcell: dict) -> None:
        """Append MemCell to memcells.jsonl."""
        with open(self.memcells_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(memcell, ensure_ascii=False) + "\n")

    def _format_conversation_for_extractors(self, original_data: list) -> str:
        """Format original_data for Episode/EventLog/Foresight prompts."""
        lines = []
        for m in original_data:
            content = m.get("content", "")
            speaker = m.get("speaker_name", m.get("role", "unknown"))
            ts = m.get("timestamp", "")[:19]
            if content:
                lines.append(f"[{ts}] {speaker}: {content}")
        return "\n".join(lines)

    async def _extract_episode(
        self, memcell: dict, provider: "LLMProvider", model: str
    ) -> dict | None:
        """Extract Episode from MemCell, append to episodes.jsonl."""
        conv = self._format_conversation_for_extractors(memcell.get("original_data", []))
        ts = memcell.get("timestamp", "")[:19]
        prompt = GROUP_EPISODE_GENERATION_PROMPT.format(
            conversation_start_time=ts,
            conversation=conv or "(无)",
            custom_instructions=DEFAULT_CUSTOM_INSTRUCTIONS,
        )
        try:
            resp = await provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                model=model,
                temperature=0.2,
            )
            text = (resp.content or "").strip()
            obj_str = _extract_json_object(text)
            if obj_str:
                try:
                    data = json.loads(obj_str)
                except json.JSONDecodeError:
                    data = json_repair.loads(obj_str)
                episode = {
                    "event_id": memcell.get("event_id"),
                    "title": data.get("title", ""),
                    "content": data.get("content", ""),
                    "summary": data.get("summary", data.get("content", "")[:200]),
                    "timestamp": memcell.get("timestamp"),
                }
                with open(self.episodes_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(episode, ensure_ascii=False) + "\n")
                return episode
        except Exception as e:
            logger.warning("Episode extraction failed: {}", e)
        return None

    async def _extract_eventlog(
        self, memcell: dict, provider: "LLMProvider", model: str
    ) -> None:
        """Extract EventLog and append to HISTORY.YYMMDD.md."""
        conv = self._format_conversation_for_extractors(memcell.get("original_data", []))
        ts = memcell.get("timestamp", "")[:19]
        prompt = EVENT_LOG_PROMPT.format(time=ts, input_text=conv or "(无)")
        try:
            resp = await provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                model=model,
                temperature=0.1,
            )
            text = (resp.content or "").strip()
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                data = json.loads(m.group())
                el = data.get("event_log", data)
                if isinstance(el, dict):
                    facts = el.get("atomic_fact", [])
                    evt_time = (el.get("time") or ts)[:16]
                elif isinstance(el, list):
                    facts = []
                    for x in el:
                        if isinstance(x, str) and x.strip():
                            facts.append(x)
                        elif isinstance(x, dict):
                            f = x.get("atomic_fact") or x.get("fact") or x.get("content")
                            if isinstance(f, str) and f.strip():
                                facts.append(f)
                    evt_time = ts[:16]
                else:
                    facts = []
                    evt_time = ts[:16]
                for fact in facts:
                    if isinstance(fact, str) and fact.strip():
                        self.append_history(f"[{evt_time}] {fact.strip()}")
        except Exception as e:
            logger.warning("EventLog extraction failed: {}", e)

    async def _extract_foresight(
        self, memcell: dict, provider: "LLMProvider", model: str
    ) -> None:
        """Extract Foresight and append to foresights.jsonl."""
        conv = self._format_conversation_for_extractors(memcell.get("original_data", []))
        prompt = FORESIGHT_GENERATION_PROMPT.format(
            user_id="user",
            user_name="用户",
            conversation_text=conv or "(无)",
        )
        try:
            resp = await provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                model=model,
                temperature=0.2,
            )
            text = (resp.content or "").strip()
            arr_match = re.search(r"\[[\s\S]*\]", text)
            if arr_match:
                items = json.loads(arr_match.group())
                for item in items:
                    if isinstance(item, dict):
                        item["event_id"] = memcell.get("event_id")
                        with open(self.foresights_file, "a", encoding="utf-8") as f:
                            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("Foresight extraction failed: {}", e)

    async def _extract_life_profile(
        self, memcell: dict, provider: "LLMProvider", model: str
    ) -> None:
        """Extract Life Profile and update USER.md (and optionally SOUL.md)."""
        conv = self._format_conversation_for_extractors(memcell.get("original_data", []))
        if not conv.strip():
            return
        user_md = self.workspace / "USER.md"
        current = user_md.read_text(encoding="utf-8") if user_md.exists() else "(空)"
        prompt = PROFILE_LIFE_UPDATE_PROMPT.format(
            current_profile=current,
            conversations=conv,
        )
        try:
            resp = await provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                model=model,
                temperature=0.2,
            )
            text = (resp.content or "").strip()
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                data = json.loads(m.group())
                ops = data.get("operations", [])
                for op in ops:
                    if op.get("action") == "add" and op.get("type") == "explicit_info":
                        d = op.get("data", {})
                        desc = d.get("description", "")
                        if desc:
                            line = f"- {desc}"
                            if not current.strip().endswith(line):
                                section = "\n\n## 对话学习 (Conversation-derived)\n"
                                if section not in current:
                                    current = current.rstrip() + section + "\n"
                                current += line + "\n"
                                user_md.parent.mkdir(parents=True, exist_ok=True)
                                user_md.write_text(current, encoding="utf-8")
        except Exception as e:
            logger.warning("Life Profile extraction failed: {}", e)

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
            if not old_messages:
                return True

        if len(old_messages) < 2:
            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            return True

        try:
            if archive_all:
                should_end, should_wait, topic_summary = True, False, "会话归档"
            else:
                # Use pending user message when available (triggered at start of turn, before it's in session)
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
                should_end, should_wait, topic_summary = await self._detect_boundary(
                    history_for_detect, new_for_detect, provider, model
                )

            if not should_end and not archive_all:
                logger.debug("EnhancedMem boundary: should_end=false, skipping consolidate (wait for more or topic change)")
                return True

            memcell = self._create_memcell(old_messages, topic_summary)
            self._append_memcell(memcell)

            assign_memcell_to_cluster(
                memcell.get("event_id", ""),
                memcell.get("timestamp", ""),
                self.cluster_state_file,
            )

            await self._extract_episode(memcell, provider, model)
            await self._extract_eventlog(memcell, provider, model)
            await self._extract_foresight(memcell, provider, model)
            await self._extract_life_profile(memcell, provider, model)

            ts = memcell.get("timestamp", datetime.now().isoformat())[:16]
            history_entry = f"[{ts}] {topic_summary}"
            self.append_history(history_entry)

            current_memory = self.read_long_term()
            if topic_summary and topic_summary != "会话归档":
                new_fact = f"- {ts}: {topic_summary}"
                updated = (current_memory.rstrip() + "\n" + new_fact + "\n") if current_memory else (new_fact + "\n")
                if new_fact not in current_memory:
                    if len(updated) > self._memory_md_max_chars:
                        updated = self._compact_memory_text(updated)
                    self.write_long_term(updated)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("EnhancedMem consolidate done: last_consolidated={}", session.last_consolidated)
            return True
        except Exception:
            logger.exception("EnhancedMem consolidate failed")
            return False
