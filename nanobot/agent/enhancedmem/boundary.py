"""Conversation boundary detection for EnhancedMem: when to split into a MemCell."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Callable

import json_repair
from loguru import logger

from nanobot.agent.enhancedmem.prompts_zh import CONV_BOUNDARY_DETECTION_PROMPT
from nanobot.agent.enhancedmem.utils import estimate_tokens

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

# Force-split limits (from EverMemOS)
HARD_TOKEN_LIMIT = 8192
HARD_MESSAGE_LIMIT = 50


def format_messages_for_prompt(messages: list[dict]) -> str:
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


def time_gap_info(history: list[dict], new_msgs: list[dict]) -> str:
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


def estimate_total_tokens(
    messages: list[dict],
    estimate_tokens_fn: Callable[[str], int] = estimate_tokens,
) -> int:
    total = 0
    for m in messages:
        content = m.get("content") or ""
        ts = str(m.get("timestamp", ""))
        total += estimate_tokens_fn(str(content) + ts)
    return total


async def detect_boundary(
    history_msgs: list[dict],
    new_msgs: list[dict],
    provider: "LLMProvider",
    model: str,
    *,
    memory_window: int | None = None,
    estimate_tokens_fn: Callable[[str], int] = estimate_tokens,
) -> tuple[bool, bool, str]:
    """Run LLM boundary detection. Returns (should_end, should_wait, topic_summary)."""
    if not new_msgs:
        return False, True, ""

    # If memory_window is provided, scale the force-split thresholds using the
    # same "messages<->tokens" ratio as the original HARD_* defaults.
    effective_message_limit = HARD_MESSAGE_LIMIT
    effective_token_limit = HARD_TOKEN_LIMIT
    if memory_window is not None:
        try:
            mw = int(memory_window)
        except (TypeError, ValueError):
            mw = None
        if mw is not None and mw > 0:
            effective_message_limit = min(HARD_MESSAGE_LIMIT, mw)
            tokens_per_message = HARD_TOKEN_LIMIT / HARD_MESSAGE_LIMIT
            effective_token_limit = min(HARD_TOKEN_LIMIT, int(tokens_per_message * mw))

    total_tokens = estimate_total_tokens(
        history_msgs + new_msgs, estimate_tokens_fn=estimate_tokens_fn
    )
    total_messages = len(history_msgs) + len(new_msgs)

    if total_tokens >= effective_token_limit or total_messages >= effective_message_limit:
        if len(history_msgs) >= 2:
            return True, False, "达到消息/Token 上限，强制切分"

    conv_history = format_messages_for_prompt(history_msgs)
    new_text = format_messages_for_prompt(new_msgs)
    time_gap = time_gap_info(history_msgs, new_msgs)

    if not history_msgs:
        return False, False, ""

    logger.debug(
        "EnhancedMem boundary detection: {} history + {} new messages",
        len(history_msgs),
        len(new_msgs),
    )
    prompt = CONV_BOUNDARY_DETECTION_PROMPT.format(
        conversation_history=conv_history or "(无)",
        time_gap_info=time_gap,
        new_messages=new_text,
    )

    for attempt in range(3):
        try:
            response = await provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "你是一名对话情节边界分析专家。仅返回JSON，不要其他说明。",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                model=model,
                temperature=0.1,
            )
            text = (response.content or "").strip()
            json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group())
                except json.JSONDecodeError:
                    data = json_repair.loads(json_match.group())
                should_end = bool(data.get("should_end", False))
                should_wait = bool(data.get("should_wait", True))
                topic_summary = str(data.get("topic_summary", "") or "")
                if should_end:
                    should_wait = False
                return should_end, should_wait, topic_summary
        except Exception as e:
            logger.warning("Boundary detection attempt {} failed: {}", attempt + 1, e)

    return False, True, ""
