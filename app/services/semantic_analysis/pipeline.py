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


def analyze_document_from_upload(*, filename: str, data: bytes) -> dict[str, object]:
    """Analyse an uploaded document, keeping nothing.

    Unlike /extract there is no artifact to hand back afterwards, so nothing is persisted:
    no object in MinIO, no file left on disk. (The PDF/DOCX parsers need a real path, so
    parse_bytes writes a temp file for the duration of the parse and removes it in a finally
    block.) The upload is the whole input, so the caller's files no longer need to be
    reachable from the server's filesystem -- which is what the old path-based contract required.
    """
    normalized_filename = str(filename or "").strip()
    if not normalized_filename:
        raise ValueError("filename is required")
    if not data:
        raise ValueError("the uploaded file is empty")

    parser_suffix = Path(normalized_filename).suffix.lower()

    text = DocumentParser().parse_bytes(data, parser_suffix)
    artifact_id = _artifact_id(normalized_filename, data)

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
        log_event(logger, "llm_extraction_skipped", filename=normalized_filename, error=str(exc))

    skills_matches: list = []
    skills_note: str | None = None
    try:
        skills_matches = extract_skills(text)
        if len(text) > MAX_CHARS:
            skills_note = f"Η ανίχνευση δεξιοτήτων εξέτασε μόνο τους πρώτους {MAX_CHARS} χαρακτήρες του εγγράφου."
    except SkillsDetectionError as exc:
        skills_note = f"Η ανίχνευση δεξιοτήτων παραλείφθηκε: {exc}"
        log_event(logger, "skills_detection_skipped", filename=normalized_filename, error=str(exc))

    log_event(
        logger,
        "document_analyzed",
        filename=normalized_filename,
        artifact_id=artifact_id,
        decision_type_confirmed=bool(decision_type and decision_type.confirmed),
        cpv_count=len(cpv_matches),
        budget_code_count=len(budget_codes),
        skills_count=len(skills_matches),
    )

    return {
        "filename": Path(normalized_filename).name or "document.bin",
        "artifact_id": artifact_id,
        "decision_type": decision_type,
        "cpv": cpv_matches,
        "budget_codes": budget_codes,
        "skills": skills_matches,
        "extraction_note": extraction_note,
        "skills_note": skills_note,
    }
