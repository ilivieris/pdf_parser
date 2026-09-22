from __future__ import annotations

import hashlib
from pathlib import Path

from document_processor_service.app.core.contracts import PostProcessing
from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.core.object_storage import ObjectNotFoundError, get_local_store
from document_processor_service.app.services.document_processing.parser import DocumentParser
from document_processor_service.app.services.document_processing.text_corrector import (
    correct_text,
    correct_text_to_markdown,
)

logger = get_logger("services.document_processor_service.extraction_pipeline")

_OUTPUT_SUFFIXES: dict[PostProcessing, str] = {
    "none": ".txt",
    "clean": "_clean.txt",
    "markdown": ".md",
}


def _artifact_id(*parts: str | bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            digest.update(part)
        else:
            digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def extract_document_from_local(
    *,
    path: str,
    post_processing: PostProcessing = "none",
) -> dict[str, object]:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        raise ValueError("path is required")

    store = get_local_store()
    try:
        data = store.get_bytes(normalized_path)
    except ObjectNotFoundError as exc:
        raise FileNotFoundError(str(exc)) from exc

    parser_suffix = Path(normalized_path).suffix.lower()
    parser = DocumentParser()
    text = parser.parse_bytes(data, parser_suffix)
    artifact_id = _artifact_id(normalized_path, data)

    log_event(
        logger,
        "document_extracted",
        path=normalized_path,
        artifact_id=artifact_id,
        char_count=len(text),
        post_processing=post_processing,
    )

    note: str | None = None
    output_text = text

    if post_processing == "clean":
        correction = correct_text(text)
        output_text = correction.text
        note = correction.note
    elif post_processing == "markdown":
        correction = correct_text_to_markdown(text)
        output_text = correction.text
        note = correction.note

    output_key = f"{artifact_id}{_OUTPUT_SUFFIXES[post_processing]}"
    output_path = store.put_bytes(key=output_key, data=output_text.encode("utf-8"))

    log_event(
        logger,
        "document_extract_finished",
        output_path=output_path,
        post_processing=post_processing,
    )

    response: dict[str, object] = {
        "filename": Path(normalized_path).name or "document.bin",
        "text_path": output_path,
    }
    if note:
        response["note"] = note
    return response
