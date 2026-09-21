from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from document_processor_service.app.core import object_storage
from document_processor_service.app.services import extraction_pipeline
from document_processor_service.app.services.document_processing.exceptions import (
    ExtractionError,
    UnsupportedFormatError,
)
from document_processor_service.app.services.document_processing.file_extractors import parse_pdf
from document_processor_service.app.services.document_processing.markdown_extractor import render_markdown_document
from document_processor_service.app.services.document_processing.parser import DocumentParser
from document_processor_service.app.services.document_processing.text_normalizer import normalize_extracted_text


def test_normalizer_preserves_plain_text_rows_tabs_and_paragraphs() -> None:
    raw = "a\tb\nc\td\n\nnext...\n10-\n20\nχαμη-\nλα"

    normalized = normalize_extracted_text(raw)

    assert "a\tb\nc\td" in normalized
    assert "\n\nnext..." in normalized
    assert "10-\n20" in normalized
    assert "χαμηλα" in normalized


def test_normalizer_joins_pdf_prose_without_losing_paragraph_breaks() -> None:
    raw = "This is\nwrapped text.\n\nThis is another\nparagraph."

    normalized = normalize_extracted_text(raw, join_line_wrapped=True)

    assert normalized == "This is wrapped text.\n\nThis is another paragraph."


def test_render_markdown_document_returns_normalized_body_without_wrapper() -> None:
    markdown = render_markdown_document(
        filename="sample.pdf",
        text="line 1\r\n\r\nline 2\r\n",
        source_uri="C:/files/sample.pdf",
        max_chars=10,
    )

    assert markdown == "line 1\n\nline 2"
    assert "# sample.pdf" not in markdown
    assert "Source:" not in markdown


def test_template1_pdf_extracts_as_markdown_friendly_text() -> None:
    sample_pdf = Path(__file__).resolve().parents[2].parent / "chatbot-template1" / "2024-540-taxtable.pdf"

    parsed = parse_pdf(sample_pdf)
    markdown = render_markdown_document(
        filename=sample_pdf.name,
        text=parsed,
        source_uri=sample_pdf.as_uri(),
    )

    assert parsed.startswith("2024 California Tax Table")
    assert "| At least 1 | But not over 1 | 1 or 3 1 | 2 or 5 1 | 4 1 | At least 2 | But not over 2 | 1 or 3 2 | 2 or 5 2 | 4 2 | At least 3 | But not over 3 | 1 or 3 3 | 2 or 5 3 | 4 3 |" in parsed
    assert "| $1 | $50 | $0 | $0 | $0 | 6,451 | 6,550 | 65 | 65 | 65 | 12,951 | 13,050 | 152 | 130 | 130 |" in parsed
    assert "## Section 1" not in markdown
    assert markdown == parsed


def test_parse_bytes_rejects_empty_suffix() -> None:
    parser = DocumentParser(supported_extensions=[".txt"])

    with pytest.raises(UnsupportedFormatError):
        parser.parse_bytes(b"hello", "")


def test_extract_uses_path_suffix_when_filename_has_no_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStore:
        def get_bytes(self, path: str) -> bytes:
            return b"doc bytes"

        def put_bytes(self, *, key: str, data: bytes) -> str:
            return f"/storage/{key}"

    class FakeParser:
        suffixes: list[str] = []

        def parse_bytes(self, data: bytes, suffix: str) -> str:
            self.suffixes.append(suffix)
            return "extracted text"

    parser = FakeParser()
    monkeypatch.setattr(extraction_pipeline, "get_local_store", lambda: FakeStore())
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: parser)

    result = extraction_pipeline.extract_document_from_local(
        path="raw/artifact/source.doc",
        filename="notes",
    )

    assert parser.suffixes == [".doc"]
    assert result["filename"] == "notes"


def test_validate_filename_hint_rejects_mismatched_suffix() -> None:
    with pytest.raises(ValueError):
        extraction_pipeline._validate_filename_hint("raw/a/source.doc", "source.pdf")


def test_extract_endpoint_sanitizes_extraction_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from document_processor_service.app import main

    def fail_extract(**kwargs: object) -> dict[str, object]:
        raise ExtractionError("leaky stderr /tmp/private/path")

    monkeypatch.setattr(main, "extract_document_from_local", fail_extract)
    response = TestClient(main.app).post("/extract", json={"path": "raw/a/file.pdf"})

    assert response.status_code == 422
    assert response.json()["detail"] == "Document text extraction failed."


def test_local_store_get_bytes_raises_not_found_for_missing_file(tmp_path: Path) -> None:
    store = object.__new__(object_storage.LocalFileStore)
    store._root = tmp_path

    with pytest.raises(object_storage.ObjectNotFoundError):
        store.get_bytes("missing/file.txt")


def test_local_store_round_trips_bytes_through_put_and_get(tmp_path: Path) -> None:
    store = object.__new__(object_storage.LocalFileStore)
    store._root = tmp_path

    stored_path = store.put_bytes(key="raw/artifact/example.txt", data=b"hello")

    assert Path(stored_path).read_bytes() == b"hello"
    assert store.get_bytes(stored_path) == b"hello"
    assert store.get_bytes("raw/artifact/example.txt") == b"hello"


def test_safe_filename_strips_path_and_normalizes_separators() -> None:
    assert extraction_pipeline._safe_filename("../nested/my report.txt") == "my-report.txt"
