"""Life Profile: parse USER.md, render, LLM update (add/update/delete), and compact."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import json_repair
from loguru import logger

from nanobot.agent.enhancedmem.prompts_zh import (
    PROFILE_LIFE_COMPACT_PROMPT,
    PROFILE_LIFE_UPDATE_PROMPT,
)

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider


def parse_life_profile_from_user_md(text: str) -> tuple[list[dict], list[dict], str]:
    """Parse Life Profile section from USER.md into explicit/implicit lists and other text."""
    if not text:
        return [], [], ""

    marker = "## Life Profile"
    start = text.find(marker)
    if start == -1:
        return [], [], text

    before = text[:start].rstrip()
    section_and_after = text[start:]

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
    current_block = None
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
        content = line.lstrip("-").strip()
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


def render_life_profile_to_markdown(
    explicit_info: list[dict], implicit_traits: list[dict]
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
            extras = []
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


def format_life_profile_for_prompt(
    explicit_info: list[dict], implicit_traits: list[dict]
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


def make_life_profile_source_tag(memcell: dict) -> str:
    """Build a YYYY-MM-DD HH:MM|event_id source tag for Life Profile."""
    raw_ts = memcell.get("timestamp") or datetime.now().isoformat()
    try:
        dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        dt = datetime.now()
    ts_str = dt.strftime("%Y-%m-%d %H:%M")
    event_id = str(memcell.get("event_id") or "unknown")
    return f"{ts_str}|{event_id}"


def truncate_life_profile_lists(
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

    keep_explicit = min(len(explicit_info), max_items)
    new_explicit = explicit_info[-keep_explicit:] if keep_explicit > 0 else []
    remaining = max_items - len(new_explicit)
    if remaining <= 0:
        return new_explicit, []
    keep_implicit = min(len(implicit_traits), remaining)
    new_implicit = implicit_traits[-keep_implicit:] if keep_implicit > 0 else []
    return new_explicit, new_implicit


async def compact_life_profile_with_llm(
    explicit_info: list[dict],
    implicit_traits: list[dict],
    provider: "LLMProvider",
    model: str,
    max_items: int,
) -> tuple[list[dict], list[dict]]:
    """Use LLM to compact Life Profile when item count exceeds limit."""
    total_items = len(explicit_info) + len(implicit_traits)
    if total_items <= max_items:
        return explicit_info, implicit_traits

    profile_text = format_life_profile_for_prompt(explicit_info, implicit_traits)
    prompt = PROFILE_LIFE_COMPACT_PROMPT.format(
        total_items=total_items,
        max_items=max_items,
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
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError:
            data = json_repair.loads(m.group())
        new_explicit = data.get("explicit_info", [])
        new_implicit = data.get("implicit_traits", [])
        if not isinstance(new_explicit, list) or not isinstance(new_implicit, list):
            raise ValueError(
                "explicit_info/implicit_traits not lists in compact result"
            )
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
        return truncate_life_profile_lists(
            explicit_info, implicit_traits, max_items
        )


async def extract_and_apply_life_profile(
    user_md_path: Path,
    memcell: dict,
    conv_text: str,
    provider: "LLMProvider",
    model: str,
    *,
    max_items: int = 80,
) -> None:
    """Extract Life Profile from conversation and update USER.md. Reads, parses, LLM update, optional compact, write back."""
    if not conv_text.strip():
        return

    if user_md_path.exists():
        raw_text = user_md_path.read_text(encoding="utf-8")
        logger.debug(
            "EnhancedMem [file READ] USER.md: {} chars (_extract_life_profile)",
            len(raw_text),
        )
    else:
        raw_text = ""

    explicit_info, implicit_traits, other_text = parse_life_profile_from_user_md(
        raw_text
    )
    current_profile_text = format_life_profile_for_prompt(
        explicit_info, implicit_traits
    )
    prompt = PROFILE_LIFE_UPDATE_PROMPT.format(
        current_profile=current_profile_text,
        conversations=conv_text,
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
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                data = json_repair.loads(m.group())
            ops = data.get("operations", [])
            if not isinstance(ops, list):
                ops = []

            source_tag = make_life_profile_source_tag(memcell)

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
                        "EnhancedMem LifeProfile op with unknown type: {}",
                        op_type,
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
                            "trait": str(
                                data_dict.get("trait", "") or ""
                            ).strip(),
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
                    raw_sources = data_dict.get("sources") or []
                    if not isinstance(raw_sources, list):
                        raw_sources = [raw_sources]
                    merged = [str(s).strip() for s in raw_sources if s]
                    merged.append(source_tag)
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
                    for key in (
                        "category",
                        "description",
                        "basis",
                        "trait",
                        "evidence",
                    ):
                        if key in data_dict and data_dict.get(key) is not None:
                            item[key] = str(data_dict.get(key) or "").strip()
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
                    deduped = []
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
                    logger.warning(
                        "EnhancedMem LifeProfile unsupported action: {}", action
                    )

            total_items = len(explicit_info) + len(implicit_traits)
            if total_items > max_items:
                explicit_info, implicit_traits = await compact_life_profile_with_llm(
                    explicit_info, implicit_traits, provider, model, max_items
                )

            profile_md = render_life_profile_to_markdown(
                explicit_info, implicit_traits
            )
            if other_text.strip():
                new_user_text = other_text.rstrip() + "\n\n" + profile_md
            else:
                new_user_text = profile_md
            user_md_path.parent.mkdir(parents=True, exist_ok=True)
            user_md_path.write_text(new_user_text, encoding="utf-8")
            logger.debug(
                "EnhancedMem [file WRITE] USER.md: {} chars (life_profile updated)",
                len(new_user_text),
            )
    except Exception as e:
        logger.warning(
            "Life Profile extraction failed (LLM response JSON parse or apply): {}", e
        )
