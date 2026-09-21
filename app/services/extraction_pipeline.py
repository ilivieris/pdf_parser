from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from document_processor_service.app.core.config import settings
from document_processor_service.app.core.object_storage import ObjectNotFoundError, get_local_store
from document_processor_service.app.services.document_processing.markdown_extractor import render_markdown_document
from document_processor_service.app.services.document_processing.parser import DocumentParser
from document_processor_service.app.services.document_processing.text_corrector import correct_text as run_text_correction


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


def _preview_text(text: str, limit: int | None = None) -> str:
    bounded = max(1, limit or settings.document_preview_chars)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) <= bounded:
        return normalized
    return f"{normalized[:bounded].rstrip()}..."


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
    export_markdown: bool = False,
    correct_text: bool = False,
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
    text_key = _join_key("extracted", artifact_id, f"{safe_stem}.txt")
    text_path = store.put_bytes(key=text_key, data=text.encode("utf-8"))

    response: dict[str, object] = {
        "filename": resolved_filename,
        "source_path": normalized_path,
        "artifact_id": artifact_id,
        "text_path": text_path,
        "char_count": len(text),
        "text_preview": _preview_text(text),
    }

    if export_markdown:
        markdown = render_markdown_document(
            filename=resolved_filename,
            text=text,
            source_uri=normalized_path,
            max_chars=settings.document_markdown_chunk_max_chars,
        )
        markdown_key = _join_key("markdown", artifact_id, f"{safe_stem}.md")
        markdown_path = store.put_bytes(key=markdown_key, data=markdown.encode("utf-8"))
        response["markdown_path"] = markdown_path
        response["markdown_char_count"] = len(markdown)

    if correct_text:
        corrected = run_text_correction(text)
        corrected_key = _join_key("corrected", artifact_id, f"{safe_stem}.txt")
        corrected_path = store.put_bytes(key=corrected_key, data=corrected.encode("utf-8"))
        response["corrected_path"] = corrected_path
        response["corrected_char_count"] = len(corrected)
        response["corrected_text_preview"] = _preview_text(corrected)

    return response
