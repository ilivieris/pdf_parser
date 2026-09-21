from __future__ import annotations

import os
import tempfile
from pathlib import Path

from document_processor_service.app.core.config import settings
from document_processor_service.app.services.document_processing.exceptions import ExtractionError, UnsupportedFormatError
from document_processor_service.app.services.document_processing.file_extractors import (
    parse_doc,
    parse_docx,
    parse_pdf,
    parse_plain_text,
)


class DocumentParser:
    def __init__(self, supported_extensions: list[str] | None = None) -> None:
        self._parsers = {
            ".pdf": parse_pdf,
            ".docx": parse_docx,
            ".doc": parse_doc,
            ".txt": parse_plain_text,
            ".md": parse_plain_text,
            ".markdown": parse_plain_text,
            ".csv": parse_plain_text,
            ".json": parse_plain_text,
            ".log": parse_plain_text,
        }
        self._supported = {ext.lower() for ext in (supported_extensions or settings.document_supported_extensions)}

    def parse(self, path: Path) -> str:
        ext = path.suffix.lower()
        self._validate_extension(ext)
        return self._dispatch(ext, path)

    def parse_bytes(self, data: bytes, suffix: str) -> str:
        normalized_suffix = suffix.lower()
        self._validate_extension(normalized_suffix)

        file_descriptor, temp_path = tempfile.mkstemp(suffix=normalized_suffix)
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(data)
            return self._dispatch(normalized_suffix, Path(temp_path))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def _validate_extension(self, ext: str) -> None:
        if not ext:
            raise UnsupportedFormatError(
                f"Unsupported format ''. Supported: {', '.join(sorted(self._supported))}"
            )
        if ext not in self._supported:
            raise UnsupportedFormatError(
                f"Unsupported format '{ext}'. Supported: {', '.join(sorted(self._supported))}"
            )

    def _dispatch(self, ext: str, path: Path) -> str:
        try:
            return self._parsers[ext](path)
        except (ExtractionError, UnsupportedFormatError):
            raise
        except Exception as exc:
            raise ExtractionError(f"Failed to extract text from '{path.name}': {exc}") from exc
