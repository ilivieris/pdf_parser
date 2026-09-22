from __future__ import annotations

import re

from document_processor_service.app.services.semantic_analysis.contracts import CpvMatch

# CPV codes are 8 digits, optionally followed by "-<check digit>" (e.g. "30192000-1").
# Diavgeia documents always write the literal "CPV" acronym next to the code, so we anchor on
# that keyword rather than matching bare 8-digit numbers, which would also catch protocol
# numbers, phone numbers, etc.
_CODE_PATTERN = re.compile(r"\b(\d{8})(?:[-‐-―]\s?(\d)\b)?")
_KEYWORD_PATTERN = re.compile(r"CPV", re.IGNORECASE)

# Full-string format check for a standalone code value (e.g. one proposed by the LLM extractor).
CODE_FORMAT_PATTERN = re.compile(r"\A\d{8}(?:-\d)?\Z")

# How far past a "CPV" keyword to keep looking for codes (covers short comma-separated lists).
_FORWARD_WINDOW = 200
_CONTEXT_WINDOW = 60


def extract_cpv(text: str) -> list[CpvMatch]:
    matches: list[CpvMatch] = []
    seen: set[str] = set()

    for keyword in _KEYWORD_PATTERN.finditer(text):
        window_end = min(len(text), keyword.end() + _FORWARD_WINDOW)
        paragraph_break = re.search(r"\n\s*\n", text[keyword.end():window_end])
        if paragraph_break:
            window_end = keyword.end() + paragraph_break.start()

        for code_match in _CODE_PATTERN.finditer(text, keyword.end(), window_end):
            digits, check_digit = code_match.group(1), code_match.group(2)
            code = f"{digits}-{check_digit}" if check_digit else digits
            if code in seen:
                continue
            seen.add(code)

            ctx_start = max(keyword.start(), code_match.start() - _CONTEXT_WINDOW)
            ctx_end = min(len(text), code_match.end() + _CONTEXT_WINDOW)
            context = " ".join(text[ctx_start:ctx_end].split())
            matches.append(CpvMatch(code=code, evidence=context, confirmed=True, source="regex"))

    return matches
