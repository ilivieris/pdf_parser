from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

from document_processor_service.app.core.config import settings
from document_processor_service.app.core.contracts import PostProcessing
from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.core.object_storage import ObjectNotFoundError, get_object_store
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

_OUTPUT_CONTENT_TYPES: dict[PostProcessing, str] = {
    "none": "text/plain; charset=utf-8",
    "clean": "text/plain; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
}

_SOURCE_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".log": "text/plain; charset=utf-8",
}

_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._\-]+")


def _artifact_id(*parts: str | bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            digest.update(part)
        else:
            digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _safe_object_name(filename: str) -> str:
    """Reduce an uploaded filename to something safe to embed in an object key.

    Strips any client-supplied directory component and collapses everything outside
    [A-Za-z0-9._-] — Greek filenames are common here and would otherwise need URL escaping
    in the presigned link.
    """
    base = PurePosixPath(filename.replace("\\", "/")).name
    cleaned = _UNSAFE_KEY_CHARS.sub("_", base).strip("._")
    return cleaned or "document.bin"


def extract_document_from_upload(
    *,
    filename: str,
    data: bytes,
    post_processing: PostProcessing = "none",
) -> dict[str, object]:
    """Upload the file to MinIO, read it back, extract, and store the result in MinIO.

    The read-back is deliberate: the bucket, not the request body, is the source of truth for
    what was parsed, so what the response points at is exactly what was processed.
    """
    normalized_filename = str(filename or "").strip()
    if not normalized_filename:
        raise ValueError("filename is required")
    if not data:
        raise ValueError("the uploaded file is empty")

    suffix = Path(normalized_filename).suffix.lower()
    artifact_id = _artifact_id(normalized_filename, data)
    store = get_object_store()

    # 1. Upload the source document into the bucket.
    source_key = f"{settings.minio_source_prefix}/{artifact_id}/{_safe_object_name(normalized_filename)}"
    store.put_bytes(
        key=source_key,
        data=data,
        content_type=_SOURCE_CONTENT_TYPES.get(suffix, "application/octet-stream"),
    )
    log_event(
        logger,
        "document_uploaded",
        bucket=store.bucket,
        source_object=source_key,
        artifact_id=artifact_id,
        byte_count=len(data),
    )

    # 2. Fetch it back from the bucket and parse it.
    try:
        stored_data = store.get_bytes(source_key)
    except ObjectNotFoundError as exc:
        raise FileNotFoundError(str(exc)) from exc

    parser = DocumentParser()
    text = parser.parse_bytes(stored_data, suffix)

    log_event(
        logger,
        "document_extracted",
        source_object=source_key,
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

    # 3. Store the result back in the bucket and hand back a download link.
    output_key = f"{settings.minio_output_prefix}/{artifact_id}{_OUTPUT_SUFFIXES[post_processing]}"
    store.put_bytes(
        key=output_key,
        data=output_text.encode("utf-8"),
        content_type=_OUTPUT_CONTENT_TYPES[post_processing],
    )
    download_url = store.presigned_url(output_key)

    # 4. Optionally drop the source now that its text is safely stored.
    source_deleted = False
    if settings.minio_delete_source_after_extract:
        store.remove_object(source_key)
        source_deleted = True

    log_event(
        logger,
        "document_extract_finished",
        bucket=store.bucket,
        source_object=source_key,
        source_deleted=source_deleted,
        output_object=output_key,
        post_processing=post_processing,
    )

    # The object keys stay out of the response on purpose: the download link is the only handle
    # a caller needs, and the keys are an internal layout detail. They are in the logs above.
    response: dict[str, object] = {
        "filename": Path(normalized_filename).name or "document.bin",
        "download_url": download_url,
    }
    if note:
        response["note"] = note
    return response
