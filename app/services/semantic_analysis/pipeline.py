from __future__ import annotations

import hashlib
from pathlib import Path

from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.services.document_processing.parser import DocumentParser
from document_processor_service.app.services.semantic_analysis.budget_code_extractor import extract_budget_codes
from document_processor_service.app.services.semantic_analysis.contracts import BudgetCodeMatch, CpvMatch
from document_processor_service.app.services.semantic_analysis.cpv_extractor import extract_cpv
from document_processor_service.app.services.semantic_analysis.exceptions import (
    LlmExtractionError,
    SemanticAnalysisError,
    SkillsDetectionError,
)
from document_processor_service.app.services.semantic_analysis.llm_extractor import extract_llm_fields
from document_processor_service.app.services.semantic_analysis.skills_extractor import MAX_CHARS, extract_skills

logger = get_logger("services.document_processor_service.semantic_analysis")


def _artifact_id(*parts: str | bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            digest.update(part)
        else:
            digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _read_bytes(path: str) -> bytes:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved

    try:
        return resolved.read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"File not found: {resolved}") from exc
    except OSError as exc:
        raise SemanticAnalysisError(f"Failed to read file '{resolved}': {exc}") from exc


def _merge_cpv(regex_matches: list[CpvMatch], llm_matches: list[CpvMatch]) -> list[CpvMatch]:
    merged = list(regex_matches)
    seen = {m.code for m in regex_matches}
    for match in llm_matches:
        if match.code in seen:
            continue
        seen.add(match.code)
        merged.append(match)
    return merged


def _merge_budget_codes(regex_matches: list[BudgetCodeMatch], llm_matches: list[BudgetCodeMatch]) -> list[BudgetCodeMatch]:
    merged = list(regex_matches)
    seen = {(m.kind, m.code) for m in regex_matches}
    for match in llm_matches:
        key = (match.kind, match.code)
        if key in seen:
            continue
        seen.add(key)
        merged.append(match)
    return merged


def analyze_document_from_local(*, path: str) -> dict[str, object]:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        raise ValueError("path is required")

    data = _read_bytes(normalized_path)
    parser_suffix = Path(normalized_path).suffix.lower()

    text = DocumentParser().parse_bytes(data, parser_suffix)
    artifact_id = _artifact_id(normalized_path, data)

    regex_cpv = extract_cpv(text)
    regex_budget_codes = extract_budget_codes(text)

    decision_type = None
    cpv_matches = regex_cpv
    budget_codes = regex_budget_codes
    extraction_note: str | None = None
    try:
        llm_result = extract_llm_fields(text)
        decision_type = llm_result["decision_type"]
        cpv_matches = _merge_cpv(regex_cpv, llm_result["cpv"])
        budget_codes = _merge_budget_codes(regex_budget_codes, llm_result["budget_codes"])
    except LlmExtractionError as exc:
        extraction_note = (
            f"Η ταξινόμηση τύπου πράξης και η LLM-based εξαγωγή CPV/ΑΛΕ παραλείφθηκαν: {exc}. "
            "Τα cpv/budget_codes παρακάτω προέρχονται μόνο από το regex πέρασμα."
        )
        log_event(logger, "llm_extraction_skipped", path=normalized_path, error=str(exc))

    skills_matches: list = []
    skills_note: str | None = None
    try:
        skills_matches = extract_skills(text)
        if len(text) > MAX_CHARS:
            skills_note = f"Η ανίχνευση δεξιοτήτων εξέτασε μόνο τους πρώτους {MAX_CHARS} χαρακτήρες του εγγράφου."
    except SkillsDetectionError as exc:
        skills_note = f"Η ανίχνευση δεξιοτήτων παραλείφθηκε: {exc}"
        log_event(logger, "skills_detection_skipped", path=normalized_path, error=str(exc))

    log_event(
        logger,
        "document_analyzed",
        path=normalized_path,
        artifact_id=artifact_id,
        decision_type_confirmed=bool(decision_type and decision_type.confirmed),
        cpv_count=len(cpv_matches),
        budget_code_count=len(budget_codes),
        skills_count=len(skills_matches),
    )

    return {
        "filename": Path(normalized_path).name or "document.bin",
        "source_path": normalized_path,
        "artifact_id": artifact_id,
        "decision_type": decision_type,
        "cpv": cpv_matches,
        "budget_codes": budget_codes,
        "skills": skills_matches,
        "extraction_note": extraction_note,
        "skills_note": skills_note,
    }
