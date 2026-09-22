from __future__ import annotations

import re

from document_processor_service.app.services.semantic_analysis.contracts import BudgetCodeMatch

# ΚΑΕ (Κωδικός Αριθμός Εξόδου) is the legacy budget-line code; ΑΛΕ (Αναλυτικός Λογαριασμός
# Εξόδων) is the newer, GFS-aligned one that replaced it from ~2018 onward. Documents use either,
# sometimes with dots between the abbreviation's letters ("Κ.Α.Ε.") or between digit groups
# ("2.420.989.001"), so both need their own tolerant patterns.
_HEADER_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ΚΑΕ", re.compile(r"Κ\.?\s?Α\.?\s?Ε\.?")),
    ("ΑΛΕ", re.compile(r"Α\.?\s?Λ\.?\s?Ε\.?")),
)
_INLINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ΚΑΕ", re.compile(r"Κ\.?\s?Α\.?\s?Ε\.?\.?\s*[:\-]?\s*(\d{2,10}(?:[.\-]\d+)*)")),
    ("ΑΛΕ", re.compile(r"Α\.?\s?Λ\.?\s?Ε\.?\.?\s*[:\-]?\s*(\d{2,10}(?:[.\-]\d+)*)")),
)
# Full-string format check for a standalone code value (e.g. one proposed by the LLM extractor).
CODE_FORMAT_PATTERN = re.compile(r"\A\d{2,10}(?:[.\-]\d+)*\Z")
_TABLE_ROW_PATTERN = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?[\s:\-]+\|[\s:\-|]+\|?\s*$")
_DESCRIPTION_HEADER_HINTS = ("ΠΕΡΙΓΡΑΦΗ", "ΟΝΟΜΑΣΙΑ", "ΑΙΤΙΟΛΟΓΙΑ")
_CONTEXT_WINDOW = 60


def _split_row(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _header_kind(cell: str) -> str | None:
    stripped = cell.strip(" .")
    for kind, pattern in _HEADER_TOKENS:
        if pattern.fullmatch(stripped):
            return kind
    return None


def _extract_from_tables(text: str) -> list[BudgetCodeMatch]:
    matches: list[BudgetCodeMatch] = []
    lines = text.splitlines()

    i = 0
    while i < len(lines):
        header_match = _TABLE_ROW_PATTERN.match(lines[i])
        if not header_match or not (i + 1 < len(lines) and _TABLE_SEPARATOR_PATTERN.match(lines[i + 1])):
            i += 1
            continue

        header_cells = _split_row(lines[i])
        code_col: int | None = None
        kind: str | None = None
        for idx, cell in enumerate(header_cells):
            found_kind = _header_kind(cell)
            if found_kind:
                code_col, kind = idx, found_kind
                break

        if code_col is None or kind is None:
            i += 2
            continue

        description_col = next(
            (
                idx
                for idx, cell in enumerate(header_cells)
                if idx != code_col and any(hint in cell.upper() for hint in _DESCRIPTION_HEADER_HINTS)
            ),
            None,
        )

        row_idx = i + 2
        while row_idx < len(lines) and _TABLE_ROW_PATTERN.match(lines[row_idx]):
            cells = _split_row(lines[row_idx])
            if code_col < len(cells) and CODE_FORMAT_PATTERN.match(cells[code_col]):
                description = (
                    cells[description_col].strip()
                    if description_col is not None and description_col < len(cells)
                    else None
                )
                matches.append(
                    BudgetCodeMatch(
                        kind=kind,
                        code=cells[code_col],
                        description=description or None,
                        evidence=" ".join(lines[row_idx].split()),
                        confirmed=True,
                        source="regex",
                    )
                )
            row_idx += 1

        i = row_idx

    return matches


def _extract_inline(text: str) -> list[BudgetCodeMatch]:
    matches: list[BudgetCodeMatch] = []
    for kind, pattern in _INLINE_PATTERNS:
        for match in pattern.finditer(text):
            code = match.group(1)
            start, end = match.span()
            ctx_start = max(0, start - _CONTEXT_WINDOW)
            ctx_end = min(len(text), end + _CONTEXT_WINDOW)
            context = " ".join(text[ctx_start:ctx_end].split())
            matches.append(
                BudgetCodeMatch(kind=kind, code=code, description=None, evidence=context, confirmed=True, source="regex")
            )
    return matches


def extract_budget_codes(text: str) -> list[BudgetCodeMatch]:
    deduped: dict[tuple[str, str], BudgetCodeMatch] = {}
    for item in _extract_from_tables(text) + _extract_inline(text):
        deduped.setdefault((item.kind, item.code), item)
    return list(deduped.values())
