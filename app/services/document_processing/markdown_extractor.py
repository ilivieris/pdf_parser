from __future__ import annotations

import re


def _normalize_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def render_markdown_document(
    *,
    filename: str,
    text: str,
    source_uri: str,
    max_chars: int = 4000,
) -> str:
    del filename, source_uri, max_chars
    return _normalize_text(text)
