"""MEMORY.md read/write, topic appending, and size-limited compression."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.enhancedmem.prompts_zh import MEMORY_COMPRESS_PROMPT

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider


class MemoryMdManager:
    """Manages memory/MEMORY.md: read, write, append topic summary, compress when over limit."""

    def __init__(
        self,
        memory_file: Path,
        max_chars: int = 6000,
        config: Any = None,
    ):
        self.memory_file = Path(memory_file)
        self._max_chars = max_chars
        self.config = config

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            content = self.memory_file.read_text(encoding="utf-8")
            logger.debug("EnhancedMem [file READ] MEMORY.md: {} chars", len(content))
            return content
        logger.debug("EnhancedMem [file READ] MEMORY.md: not exists")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        self.memory_file.write_text(content, encoding="utf-8")
        logger.debug("EnhancedMem [file WRITE] MEMORY.md: {} chars", len(content))

    def compact_memory_text(self, content: str) -> str:
        """Truncate memory to stay under max chars, keeping most recent facts. Fallback when LLM compression is not used or fails."""
        if len(content) <= self._max_chars:
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
        keep = self._max_chars // 80
        kept_facts = facts[-keep:] if len(facts) > keep else facts
        result = "\n".join(header + [""] + kept_facts) + "\n"
        return result[: self._max_chars]

    async def compact_memory_with_llm(
        self, content: str, provider: "LLMProvider", model: str
    ) -> str:
        """Ask LLM to compress MEMORY.md: keep important facts, merge redundant, remove low-value, stay under max chars. Fallback to compact_memory_text on failure."""
        if len(content) <= self._max_chars:
            return content
        prompt = MEMORY_COMPRESS_PROMPT.format(
            max_chars=self._max_chars,
            content=content,
        )
        try:
            resp = await provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "你只输出压缩后的 Markdown 内容，不要任何解释或代码块包裹。",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                model=model,
                temperature=0.2,
            )
            text = (resp.content or "").strip()
            for start in ("```markdown\n", "```md\n", "```\n"):
                if text.startswith(start):
                    text = text[len(start) :].strip()
            if text.endswith("```"):
                text = text[:-3].strip()
            if len(text) <= self._max_chars and len(text) > 0:
                logger.info(
                    "EnhancedMem MEMORY.md compressed by LLM: {} -> {} chars",
                    len(content),
                    len(text),
                )
                return text
        except Exception as e:
            logger.warning(
                "MEMORY.md LLM compression failed, using truncation fallback: {}", e
            )
        return self.compact_memory_text(content)

    async def append_topic_summary(
        self,
        ts: str,
        topic_summary: str,
        provider: "LLMProvider",
        model: str,
        *,
        archive_all: bool = False,
    ) -> None:
        """Append a topic summary line to MEMORY.md if valid. Skip when archive_all (e.g. /new). Optionally compress if over limit."""
        if not topic_summary or topic_summary == "会话归档":
            return
        current_memory = self.read_long_term()
        new_fact = f"- {ts}: {topic_summary}"
        if new_fact in current_memory:
            return
        updated = (
            (current_memory.rstrip() + "\n" + new_fact + "\n")
            if current_memory
            else (new_fact + "\n")
        )
        if len(updated) > self._max_chars:
            updated = await self.compact_memory_with_llm(updated, provider, model)
        self.write_long_term(updated)
