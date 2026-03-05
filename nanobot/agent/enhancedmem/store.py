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
    MEMORY_COMPRESS_PROMPT,
    PROFILE_LIFE_COMPACT_PROMPT,
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
        self._life_profile_max_items = (
            getattr(config, "life_profile_max_items", None) or 80
        )

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            content = self.memory_file.read_text(encoding="utf-8")
            logger.debug("EnhancedMem [file READ] MEMORY.md: {} chars", len(content))
            return content
        logger.debug("EnhancedMem [file READ] MEMORY.md: not exists")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")
        logger.debug("EnhancedMem [file WRITE] MEMORY.md: {} chars", len(content))

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

        # Query-based retrieval from HISTORY.YYMMDD.md (EventLog)
        history_hits = []
        if query and len(query.strip()) >= 2:
            history_hits = self._retrieve_history(query=query.strip(), limit=5)
            if history_hits:
                parts.append("## Relevant History\n" + "\n".join(history_hits))

        if parts:
            query_preview = (query or "")[:50] + ("..." if len(query or "") > 50 else "")
            logger.info(
                "EnhancedMem context loaded: MEMORY={}ch, episodes={} ({}){}, history={}",
                len(long_term), len(episodes), ep_mode, f", query=\"{query_preview}\"" if query else "", len(history_hits),
            )

        if not parts:
            return ""
        return "\n\n".join(parts)

    def _retrieve_history(self, query: str, limit: int = 5) -> list[str]:
        """Retrieve EventLog lines matching query keywords from HISTORY.YYMMDD.md."""
        terms = [t for t in query.split() if len(t) >= 2]
        if not terms:
            return []
        hits = []
        for path in sorted(self.memory_dir.glob("HISTORY.*.md"), reverse=True)[:14]:
            try:
                text = path.read_text(encoding="utf-8")
                logger.debug("EnhancedMem [file READ] {}: {} lines (retrieve_history)", path.name, len(text.splitlines()))
                for line in text.splitlines():
                    if any(t in line for t in terms):
                        hits.append(line.strip())
                        if len(hits) >= limit:
                            return hits
            except OSError:
                continue
        return hits

    def _retrieve_episodes(self, query: str | None = None, limit: int = 5) -> list[dict]:
        """Retrieve episodes by keyword match with query, or recent N if no query/no matches."""
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

        # No query or too short: use most recent
        terms = []
        if query and len(query.strip()) >= 2:
            terms = [t for t in query.strip().split() if len(t) >= 2]
        if not terms:
            return list(reversed(episodes))[-limit:]

        # Score by keyword overlap in title + summary + content
        def score(ep: dict) -> int:
            text = " ".join(
                str(ep.get(k, "")) for k in ("title", "summary", "content")
            )
            return sum(1 for t in terms if t in text)

        scored = [(ep, score(ep)) for ep in episodes]
        scored.sort(key=lambda x: (-x[1], x[0].get("timestamp", "") or ""))
        chosen = [ep for ep, s in scored if s > 0][:limit]
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
        """Truncate memory to stay under max chars, keeping most recent facts. Fallback when LLM compression is not used or fails."""
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

    async def _compact_memory_with_llm(
        self, content: str, provider: "LLMProvider", model: str
    ) -> str:
        """Ask LLM to compress MEMORY.md: keep important facts, merge redundant, remove low-value, stay under max chars. Fallback to _compact_memory_text on failure."""
        if len(content) <= self._memory_md_max_chars:
            return content
        prompt = MEMORY_COMPRESS_PROMPT.format(
            max_chars=self._memory_md_max_chars,
            content=content,
        )
        try:
            resp = await provider.chat(
                messages=[
                    {"role": "system", "content": "你只输出压缩后的 Markdown 内容，不要任何解释或代码块包裹。"},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                model=model,
                temperature=0.2,
            )
            text = (resp.content or "").strip()
            # Strip common code-block wrappers if model ignored instruction
            for start in ("```markdown\n", "```md\n", "```\n"):
                if text.startswith(start):
                    text = text[len(start) :].strip()
            if text.endswith("```"):
                text = text[:-3].strip()
            if len(text) <= self._memory_md_max_chars and len(text) > 0:
                logger.info("EnhancedMem MEMORY.md compressed by LLM: {} -> {} chars", len(content), len(text))
                return text
        except Exception as e:
            logger.warning("MEMORY.md LLM compression failed, using truncation fallback: {}", e)
        return self._compact_memory_text(content)

    def _append_memcell(self, memcell: dict) -> None:
        """Append MemCell to memcells.jsonl."""
        line = json.dumps(memcell, ensure_ascii=False) + "\n"
        with open(self.memcells_file, "a", encoding="utf-8") as f:
            f.write(line)
        logger.debug("EnhancedMem [file APPEND] memcells.jsonl: event_id={}", memcell.get("event_id", "?"))

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
                logger.debug("EnhancedMem [file APPEND] episodes.jsonl: event_id={} title=\"{}\"", memcell.get("event_id", "?"), (data.get("title", "") or "")[:40])
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
                        logger.debug("EnhancedMem [file APPEND] foresights.jsonl: event_id={}", memcell.get("event_id", "?"))
        except Exception as e:
            logger.warning("Foresight extraction failed: {}", e)

    def _parse_life_profile_from_user_md(
        self, text: str
    ) -> tuple[list[dict], list[dict], str]:
        """Parse Life Profile section from USER.md into explicit/implicit lists and other text."""
        if not text:
            return [], [], ""

        # Locate Life Profile section by heading
        marker = "## Life Profile"
        start = text.find(marker)
        if start == -1:
            # No structured section yet, treat whole file as other_text
            return [], [], text

        before = text[:start].rstrip()
        section_and_after = text[start:]

        # Find next top-level heading (## ...) to delimit Life Profile block
        m = re.search(r"\n##\s+", section_and_after[len(marker) :])
        if m:
            end = start + len(marker) + m.start()
        else:
            end = len(text)

        section = text[start:end]
        after = text[end:].lstrip("\n")
        other_parts = []
        if before:
            other_parts.append(before)
        if after:
            other_parts.append(after)
        other_text = ("\n\n".join(other_parts)).rstrip()

        explicit_info: list[dict] = []
        implicit_traits: list[dict] = []

        lines = section.splitlines()
        current_block = None  # "explicit" | "implicit" | None
        for raw in lines:
            line = raw.strip()
            if line.startswith("###"):
                if "显性信息" in line or "explicit_info" in line:
                    current_block = "explicit"
                elif "隐性特质" in line or "implicit_traits" in line:
                    current_block = "implicit"
                else:
                    current_block = None
                continue
            if not line.startswith("-"):
                continue
            # Bullet line, try to parse basic fields
            content = line.lstrip("-").strip()
            # Drop leading [index]
            content = re.sub(r"^\[\d+\]\s*", "", content)
            if current_block == "explicit":
                item: dict[str, object] = {
                    "category": "",
                    "description": content,
                    "evidence": "",
                    "sources": [],
                }
                explicit_info.append(item)
            elif current_block == "implicit":
                trait = ""
                desc_part = content
                if content.startswith("[") and "]" in content:
                    end_idx = content.find("]")
                    trait = content[1:end_idx].strip()
                    desc_part = content[end_idx + 1 :].strip()
                item = {
                    "trait": trait,
                    "description": desc_part,
                    "basis": "",
                    "evidence": "",
                    "sources": [],
                }
                implicit_traits.append(item)

        logger.debug(
            "EnhancedMem LifeProfile parse: explicit={}, implicit={}, other_text_len={}",
            len(explicit_info),
            len(implicit_traits),
            len(other_text),
        )
        return explicit_info, implicit_traits, other_text

    def _render_life_profile_to_markdown(
        self, explicit_info: list[dict], implicit_traits: list[dict]
    ) -> str:
        """Render Life Profile lists to Markdown section."""
        lines: list[str] = []
        lines.append("## Life Profile（生活画像）")
        lines.append("")
        lines.append("### 显性信息 (explicit_info)")
        if not explicit_info:
            lines.append("- _暂无显性信息_")
        else:
            for idx, item in enumerate(explicit_info):
                category = str(item.get("category", "") or "").strip()
                description = str(item.get("description", "") or "").strip()
                evidence = str(item.get("evidence", "") or "").strip()
                sources = item.get("sources") or []
                if not isinstance(sources, list):
                    sources = [str(sources)]
                sources_str = ", ".join(str(s) for s in sources if s)

                parts: list[str] = []
                if category:
                    parts.append(f"[{category}]")
                if description:
                    parts.append(description)
                extras: list[str] = []
                if evidence:
                    extras.append(f"evidence: {evidence}")
                if sources_str:
                    extras.append(f"sources: {sources_str}")
                text = " ".join(parts) if parts else ""
                if extras:
                    if text:
                        text = f"{text} —— " + " | ".join(extras)
                    else:
                        text = " | ".join(extras)
                lines.append(f"- [{idx}] {text}".rstrip())

        lines.append("")
        lines.append("### 隐性特质 (implicit_traits)")
        if not implicit_traits:
            lines.append("- _暂无隐性特质_")
        else:
            for idx, item in enumerate(implicit_traits):
                trait = str(item.get("trait", "") or "").strip()
                description = str(item.get("description", "") or "").strip()
                evidence = str(item.get("evidence", "") or "").strip()
                sources = item.get("sources") or []
                if not isinstance(sources, list):
                    sources = [str(sources)]
                sources_str = ", ".join(str(s) for s in sources if s)

                label = f"[{trait}]" if trait else ""
                base = " ".join(x for x in (label, description) if x).strip()
                extras: list[str] = []
                basis = str(item.get("basis", "") or "").strip()
                if basis:
                    extras.append(f"basis: {basis}")
                if evidence:
                    extras.append(f"evidence: {evidence}")
                if sources_str:
                    extras.append(f"sources: {sources_str}")
                if extras:
                    if base:
                        base = f"{base} —— " + " | ".join(extras)
                    else:
                        base = " | ".join(extras)
                lines.append(f"- [{idx}] {base}".rstrip())

        md = "\n".join(lines).rstrip() + "\n"
        logger.debug(
            "EnhancedMem LifeProfile render: explicit={}, implicit={}, md_len={}",
            len(explicit_info),
            len(implicit_traits),
            len(md),
        )
        return md

    def _format_life_profile_for_prompt(
        self, explicit_info: list[dict], implicit_traits: list[dict]
    ) -> str:
        """Format Life Profile lists into text with explicit indexes for LLM prompt."""
        lines: list[str] = []
        if not explicit_info and not implicit_traits:
            return "（当前暂无结构化画像）"

        lines.append("explicit_info:")
        if explicit_info:
            for idx, item in enumerate(explicit_info):
                category = str(item.get("category", "") or "").strip()
                description = str(item.get("description", "") or "").strip()
                evidence = str(item.get("evidence", "") or "").strip()
                sources = item.get("sources") or []
                if not isinstance(sources, list):
                    sources = [str(sources)]
                src = ", ".join(str(s) for s in sources if s)
                lines.append(
                    f"[{idx}] category={category!r}, description={description!r}, evidence={evidence!r}, sources=[{src}]"
                )
        else:
            lines.append("(none)")

        lines.append("")
        lines.append("implicit_traits:")
        if implicit_traits:
            for idx, item in enumerate(implicit_traits):
                trait = str(item.get("trait", "") or "").strip()
                description = str(item.get("description", "") or "").strip()
                basis = str(item.get("basis", "") or "").strip()
                evidence = str(item.get("evidence", "") or "").strip()
                sources = item.get("sources") or []
                if not isinstance(sources, list):
                    sources = [str(sources)]
                src = ", ".join(str(s) for s in sources if s)
                lines.append(
                    f"[{idx}] trait={trait!r}, description={description!r}, basis={basis!r}, evidence={evidence!r}, sources=[{src}]"
                )
        else:
            lines.append("(none)")
        return "\n".join(lines)

    def _make_life_profile_source_tag(self, memcell: dict) -> str:
        """Build a YYYY-MM-DD HH:MM|event_id source tag for Life Profile."""
        raw_ts = memcell.get("timestamp") or datetime.now().isoformat()
        try:
            dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            dt = datetime.now()
        ts_str = dt.strftime("%Y-%m-%d %H:%M")
        event_id = str(memcell.get("event_id") or "unknown")
        return f"{ts_str}|{event_id}"

    def _truncate_life_profile_lists(
        self,
        explicit_info: list[dict],
        implicit_traits: list[dict],
        max_items: int,
    ) -> tuple[list[dict], list[dict]]:
        """Simple fallback truncation when compacting Life Profile fails."""
        if max_items <= 0:
            return [], []
        total = len(explicit_info) + len(implicit_traits)
        if total <= max_items:
            return explicit_info, implicit_traits

        # 简单策略：先裁剪显性信息数量，若仍超出再裁剪隐性特质
        keep_explicit = min(len(explicit_info), max_items)
        new_explicit = explicit_info[-keep_explicit:] if keep_explicit > 0 else []
        remaining = max_items - len(new_explicit)
        if remaining <= 0:
            return new_explicit, []
        keep_implicit = min(len(implicit_traits), remaining)
        new_implicit = implicit_traits[-keep_implicit:] if keep_implicit > 0 else []
        return new_explicit, new_implicit

    async def _compact_life_profile_with_llm(
        self,
        explicit_info: list[dict],
        implicit_traits: list[dict],
        provider: "LLMProvider",
        model: str,
    ) -> tuple[list[dict], list[dict]]:
        """Use LLM to compact Life Profile when item count exceeds limit."""
        total_items = len(explicit_info) + len(implicit_traits)
        if total_items <= self._life_profile_max_items:
            return explicit_info, implicit_traits

        profile_text = self._format_life_profile_for_prompt(
            explicit_info, implicit_traits
        )
        prompt = PROFILE_LIFE_COMPACT_PROMPT.format(
            total_items=total_items,
            max_items=self._life_profile_max_items,
            profile_text=profile_text,
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
            if not m:
                raise ValueError("No JSON object found in compact response")
            data = json.loads(m.group())
            new_explicit = data.get("explicit_info", [])
            new_implicit = data.get("implicit_traits", [])
            if not isinstance(new_explicit, list) or not isinstance(
                new_implicit, list
            ):
                raise ValueError("explicit_info/implicit_traits not lists in compact result")
            logger.info(
                "EnhancedMem LifeProfile compacted by LLM: {} -> {} items",
                total_items,
                len(new_explicit) + len(new_implicit),
            )
            return new_explicit, new_implicit
        except Exception as e:
            logger.warning(
                "LifeProfile LLM compact failed, using truncation fallback: {}", e
            )
            return self._truncate_life_profile_lists(
                explicit_info, implicit_traits, self._life_profile_max_items
            )

    async def _extract_life_profile(
        self, memcell: dict, provider: "LLMProvider", model: str
    ) -> None:
        """Extract Life Profile and update USER.md (and optionally SOUL.md)."""
        conv = self._format_conversation_for_extractors(memcell.get("original_data", []))
        if not conv.strip():
            return
        user_md = self.workspace / "USER.md"
        if user_md.exists():
            raw_text = user_md.read_text(encoding="utf-8")
            logger.debug(
                "EnhancedMem [file READ] USER.md: {} chars (_extract_life_profile)",
                len(raw_text),
            )
        else:
            raw_text = ""

        explicit_info, implicit_traits, other_text = self._parse_life_profile_from_user_md(
            raw_text
        )
        current_profile_text = self._format_life_profile_for_prompt(
            explicit_info, implicit_traits
        )
        prompt = PROFILE_LIFE_UPDATE_PROMPT.format(
            current_profile=current_profile_text,
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
                if not isinstance(ops, list):
                    ops = []

                source_tag = self._make_life_profile_source_tag(memcell)

                for op in ops:
                    action = str(op.get("action", "")).lower()
                    if action == "none":
                        logger.debug("EnhancedMem LifeProfile op: none")
                        continue

                    op_type = str(op.get("type", "")).lower()
                    if op_type == "explicit_info":
                        target_list = explicit_info
                    elif op_type == "implicit_traits":
                        target_list = implicit_traits
                    else:
                        logger.warning(
                            "EnhancedMem LifeProfile op with unknown type: {}", op_type
                        )
                        continue

                    if action == "add":
                        data_dict = op.get("data") or {}
                        if op_type == "explicit_info":
                            item = {
                                "category": str(
                                    data_dict.get("category", "") or ""
                                ).strip(),
                                "description": str(
                                    data_dict.get("description", "") or ""
                                ).strip(),
                                "evidence": str(
                                    data_dict.get("evidence", "") or ""
                                ).strip(),
                                "sources": [],
                            }
                        else:
                            item = {
                                "trait": str(data_dict.get("trait", "") or "").strip(),
                                "description": str(
                                    data_dict.get("description", "") or ""
                                ).strip(),
                                "basis": str(
                                    data_dict.get("basis", "") or ""
                                ).strip(),
                                "evidence": str(
                                    data_dict.get("evidence", "") or ""
                                ).strip(),
                                "sources": [],
                            }
                        # Merge sources from op and add current source_tag
                        raw_sources = data_dict.get("sources") or []
                        if not isinstance(raw_sources, list):
                            raw_sources = [raw_sources]
                        merged = [str(s).strip() for s in raw_sources if s]
                        merged.append(source_tag)
                        # Deduplicate while preserving order
                        seen: set[str] = set()
                        deduped: list[str] = []
                        for s in merged:
                            if s not in seen:
                                seen.add(s)
                                deduped.append(s)
                        item["sources"] = deduped
                        target_list.append(item)
                    elif action == "update":
                        try:
                            index = int(op.get("index", -1))
                        except (TypeError, ValueError):
                            index = -1
                        if index < 0 or index >= len(target_list):
                            logger.warning(
                                "EnhancedMem LifeProfile update index out of range: {} (len={})",
                                index,
                                len(target_list),
                            )
                            continue
                        data_dict = op.get("data") or {}
                        item = dict(target_list[index])
                        # Merge scalar fields
                        for key in ("category", "description", "basis", "trait", "evidence"):
                            if key in data_dict and data_dict.get(key) is not None:
                                item[key] = str(data_dict.get(key) or "").strip()
                        # Merge sources from old/new and add current source_tag
                        old_sources = item.get("sources") or []
                        if not isinstance(old_sources, list):
                            old_sources = [old_sources]
                        new_sources = data_dict.get("sources") or []
                        if not isinstance(new_sources, list):
                            new_sources = [new_sources]
                        merged = (
                            [str(s).strip() for s in old_sources if s]
                            + [str(s).strip() for s in new_sources if s]
                        )
                        merged.append(source_tag)
                        seen = set()
                        deduped: list[str] = []
                        for s in merged:
                            if s and s not in seen:
                                seen.add(s)
                                deduped.append(s)
                        item["sources"] = deduped
                        target_list[index] = item
                    elif action == "delete":
                        try:
                            index = int(op.get("index", -1))
                        except (TypeError, ValueError):
                            index = -1
                        if index < 0 or index >= len(target_list):
                            logger.warning(
                                "EnhancedMem LifeProfile delete index out of range: {} (len={})",
                                index,
                                len(target_list),
                            )
                            continue
                        target_list.pop(index)
                    else:
                        logger.warning("EnhancedMem LifeProfile unsupported action: {}", action)

                # Capacity control and optional compact
                total_items = len(explicit_info) + len(implicit_traits)
                if total_items > self._life_profile_max_items:
                    explicit_info, implicit_traits = await self._compact_life_profile_with_llm(
                        explicit_info, implicit_traits, provider, model
                    )

                # Render and write USER.md
                profile_md = self._render_life_profile_to_markdown(
                    explicit_info, implicit_traits
                )
                if other_text.strip():
                    new_user_text = other_text.rstrip() + "\n\n" + profile_md
                else:
                    new_user_text = profile_md
                user_md.parent.mkdir(parents=True, exist_ok=True)
                user_md.write_text(new_user_text, encoding="utf-8")
                logger.debug(
                    "EnhancedMem [file WRITE] USER.md: {} chars (life_profile updated)",
                    len(new_user_text),
                )
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
                        updated = await self._compact_memory_with_llm(updated, provider, model)
                    self.write_long_term(updated)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info("EnhancedMem consolidate done: last_consolidated={}", session.last_consolidated)
            return True
        except Exception:
            logger.exception("EnhancedMem consolidate failed")
            return False
