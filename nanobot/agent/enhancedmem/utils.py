"""Shared utilities for EnhancedMem: token estimate, JSON extraction, history paths."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

# Approximate tokens from chars (for languages without tiktoken)
CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def extract_json_object(text: str) -> str | None:
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


def history_path_for_date(memory_dir: Path, dt: datetime) -> Path:
    """Get HISTORY.YYMMDD.md path for a date."""
    return memory_dir / f"HISTORY.{dt.strftime('%y%m%d')}.md"
