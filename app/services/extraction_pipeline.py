from __future__ import annotations

import hashlib
import re
import unicodedata
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


def _safe_filename(filename: str) -> str:
    name = re.split(r"[\\/]+", filename)[-1] or "document.bin"
    normalized = unicodedata.normalize("NFC", name)
    slug = re.sub(r"[^\w.\-]+", "-", normalized, flags=re.UNICODE)
    return re.sub(r"-{2,}", "-", slug).strip(".-_") or "document.bin"


def _safe_stem(filename: str) -> str:
    stem = Path(_safe_filename(filename)).stem or "document"
    slug = re.sub(r"[^\w.\-]+", "-", stem, flags=re.UNICODE)
    return re.sub(r"-{2,}", "-", slug).strip(".-_") or "document"


def _join_key(*parts: str) -> str:
    return "/".join(part for part in (str(item).strip().strip("/") for item in parts) if part)


def _artifact_id(*parts: str | bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            digest.update(part)
        else:
            digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _validate_filename_hint(path: str, filename: str | None) -> None:
    if not filename:
        return

    path_suffix = Path(path).suffix.lower()
    filename_suffix = Path(filename).suffix.lower()
    if path_suffix and filename_suffix and path_suffix != filename_suffix:
        raise ValueError("filename extension must match path extension")


def extract_document_from_local(
    *,
    path: str,
    post_processing: PostProcessing = "none",
    filename: str | None = None,
) -> dict[str, object]:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        raise ValueError("path is required")

    _validate_filename_hint(normalized_path, filename)

    store = get_local_store()
    try:
        data = store.get_bytes(normalized_path)
    except ObjectNotFoundError as exc:
        raise FileNotFoundError(str(exc)) from exc

    resolved_filename = filename or Path(normalized_path).name or "document.bin"
    path_suffix = Path(normalized_path).suffix.lower()
    filename_suffix = Path(resolved_filename).suffix.lower()
    parser_suffix = filename_suffix or path_suffix
    parser = DocumentParser()
    text = parser.parse_bytes(data, parser_suffix)
    artifact_id = _artifact_id(normalized_path, data)
    safe_stem = _safe_stem(resolved_filename)

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
    output_kind = "extracted"
    extension = ".txt"

    if post_processing == "clean":
        correction = correct_text(text)
        output_text = correction.text
        note = correction.note
        output_kind = "clean"
    elif post_processing == "markdown":
        correction = correct_text_to_markdown(text)
        output_text = correction.text
        note = correction.note
        output_kind = "markdown"
        extension = ".md"

    output_key = _join_key(output_kind, artifact_id, f"{safe_stem}{extension}")
    output_path = store.put_bytes(key=output_key, data=output_text.encode("utf-8"))

    log_event(
        logger,
        "document_extract_finished",
        path=normalized_path,
        artifact_id=artifact_id,
        output_path=output_path,
        post_processing=post_processing,
    )

    response: dict[str, object] = {
        "filename": resolved_filename,
        "source_path": normalized_path,
        "artifact_id": artifact_id,
        "text_path": output_path,
    }
    if note:
        response["note"] = note
    return response
