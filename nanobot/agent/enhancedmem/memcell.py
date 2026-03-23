"""MemCell creation, persistence, and Episode/EventLog/Foresight extraction."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import json_repair
from loguru import logger

from nanobot.agent.enhancedmem.prompts_zh import (
    DEFAULT_CUSTOM_INSTRUCTIONS,
    EVENT_LOG_PROMPT,
    FORESIGHT_GENERATION_PROMPT,
    GROUP_EPISODE_GENERATION_PROMPT,
)
from nanobot.agent.enhancedmem.utils import extract_json_object

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider


def create_memcell(messages: list[dict], summary: str) -> dict:
    """Create MemCell dict from messages."""
    last_ts = (
        messages[-1].get("timestamp", datetime.now().isoformat())
        if messages
        else datetime.now().isoformat()
    )
    try:
        dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        dt = datetime.now()

    sanitized_summary = summary or "对话片段"
    # Internal chunking reasons are not useful to keep in long-term memory
    # (they describe consolidation mechanics, not user knowledge).
    if (
        "强制切分" in sanitized_summary
        or sanitized_summary in ("会话归档",)
        or "达到 memory_window 上限" in sanitized_summary
    ):
        sanitized_summary = "对话片段"

    return {
        "event_id": str(uuid.uuid4()),
        "original_data": [
            {
                "role": m.get("role", "user"),
                "content": m.get("content", ""),
                "timestamp": m.get("timestamp", ""),
                "speaker_name": m.get("role", "user").upper(),
            }
            for m in messages
            if m.get("content")
        ],
        "timestamp": dt.isoformat(),
        "summary": sanitized_summary,
        "participants": list({m.get("role", "user") for m in messages}),
        "type": "conversation",
    }


def append_memcell(memcell: dict, memcells_file: Path) -> None:
    """Append MemCell to memcells.jsonl."""
    memcells_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(memcell, ensure_ascii=False) + "\n"
    with open(memcells_file, "a", encoding="utf-8") as f:
        f.write(line)
    logger.debug(
        "EnhancedMem [file APPEND] memcells.jsonl: event_id={}",
        memcell.get("event_id", "?"),
    )


def format_conversation_for_extractors(original_data: list) -> str:
    """Format original_data for Episode/EventLog/Foresight prompts."""
    lines = []
    for m in original_data:
        content = m.get("content", "")
        speaker = m.get("speaker_name", m.get("role", "unknown"))
        ts = m.get("timestamp", "")[:19]
        if content:
            lines.append(f"[{ts}] {speaker}: {content}")
    return "\n".join(lines)


async def extract_episode(
    memcell: dict,
    provider: "LLMProvider",
    model: str,
    episodes_file: Path,
) -> dict | None:
    """Extract Episode from MemCell, append to episodes.jsonl. Returns episode dict or None."""
    conv = format_conversation_for_extractors(memcell.get("original_data", []))
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
        obj_str = extract_json_object(text)
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
            episodes_file.parent.mkdir(parents=True, exist_ok=True)
            with open(episodes_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(episode, ensure_ascii=False) + "\n")
            logger.debug(
                'EnhancedMem [file APPEND] episodes.jsonl: event_id={} title="{}"',
                memcell.get("event_id", "?"),
                (data.get("title", "") or "")[:40],
            )
            return episode
    except Exception as e:
        logger.warning("Episode extraction failed: {}", e)
    return None


async def extract_eventlog(
    memcell: dict,
    provider: "LLMProvider",
    model: str,
    append_history: Callable[[str], None],
) -> None:
    """Extract EventLog and append to HISTORY.YYMMDD.md via append_history."""
    conv = format_conversation_for_extractors(memcell.get("original_data", []))
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
        write_out_facts = []
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                data = json_repair.loads(m.group())
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
                        f = (
                            x.get("atomic_fact")
                            or x.get("fact")
                            or x.get("content")
                        )
                        if isinstance(f, str) and f.strip():
                            facts.append(f)
                evt_time = ts[:16]
            else:
                facts = []
                evt_time = ts[:16]
            for fact in facts:
                if isinstance(fact, str) and fact.strip():
                    write_out_facts.append(f"[{evt_time}] {fact.strip()}")
    except Exception as e:
        logger.warning("EventLog extraction failed: {}", e)

    if write_out_facts:
        append_history(write_out_facts)


async def extract_foresight(
    memcell: dict,
    provider: "LLMProvider",
    model: str,
    foresights_file: Path,
) -> None:
    """Extract Foresight and append to foresights.jsonl."""
    conv = format_conversation_for_extractors(memcell.get("original_data", []))
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
            try:
                items = json.loads(arr_match.group())
            except json.JSONDecodeError:
                items = json_repair.loads(arr_match.group())
            if not isinstance(items, list):
                items = []
            foresights_file.parent.mkdir(parents=True, exist_ok=True)
            __flush_out_count = 0
            for item in items:
                if isinstance(item, dict):
                    item["event_id"] = memcell.get("event_id")
                    with open(foresights_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
                    __flush_out_count += 1
            logger.debug(
                "EnhancedMem [file APPEND] foresights.jsonl with {} lines: event_id={}",
                __flush_out_count, memcell.get("event_id", "?"),
            )
    except Exception as e:
        logger.warning("Foresight extraction failed: {}", e)
