from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from document_processor_service.app.core import object_storage
from document_processor_service.app.core.config import settings
from document_processor_service.app.services import extraction_pipeline
from document_processor_service.app.services.document_processing import text_corrector
from document_processor_service.app.services.document_processing.exceptions import (
    ExtractionError,
    TextCorrectionError,
    UnsupportedFormatError,
)
from document_processor_service.app.services.document_processing.file_extractors import parse_pdf
from document_processor_service.app.services.document_processing.markdown_extractor import render_markdown_document
from document_processor_service.app.services.document_processing.parser import DocumentParser
from document_processor_service.app.services.document_processing.text_corrector import CorrectionResult
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
    if not sample_pdf.exists():
        pytest.skip(f"fixture lives in a sibling repo not present here: {sample_pdf}")

    parsed = parse_pdf(sample_pdf)
    markdown = render_markdown_document(
        filename=sample_pdf.name,
        text=parsed,
        source_uri=sample_pdf.as_uri(),
    )

    assert parsed.startswith("2024 California Tax Table")
    assert "|" in parsed  # the tax bracket grid is rendered as a markdown table
    assert "$1" in parsed and "$50" in parsed
    assert "## Section 1" not in markdown
    assert markdown == parsed


def test_diavgeia_pdf_extracts_label_value_table_without_mangling_prose() -> None:
    pdf_dir = Path(__file__).resolve().parents[1] / "diavgeia_sample" / "pdf"
    if not pdf_dir.is_dir():
        pytest.skip(f"sample corpus not present in this environment: {pdf_dir}")

    prefix = "6" + "Γ" + "Ι" + "1" + "Ο" + "Ρ" + "1" + "Π"
    sample_pdf = next((p for p in pdf_dir.glob("*.pdf") if p.stem.startswith(prefix)), None)
    if sample_pdf is None:
        pytest.skip(f"sample file not present under {pdf_dir}")

    parsed = parse_pdf(sample_pdf)

    assert "| Προθεσμία Παραλαβής Προσφορών | Έως Δευτέρα 11 Μαΐου 2020 |" in parsed
    assert "ΛΟΙΠΟΙ ΟΡΟΙ" in parsed
    # Text before a table on the same page must keep its own line breaks
    # (label/value fields), not collapse into one run-on line.
    assert "ΕΛΛΗΝΙΚΗ ΔΗΜΟΚΡΑΤΙΑ\nΑΝΑΡΤΗΤΕΟ ΣΤΟ ΔΙΑΔΙΚΤΥΟ\nΥΠΟΥΡΓΕΙΟ ΥΓΕΙΑΣ" in parsed
    # Plain bulleted prose on the last page must not be misdetected as a table.
    assert "| - |" not in parsed


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


def test_extract_with_clean_post_processing_writes_corrected_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.writes: dict[str, bytes] = {}

        def get_bytes(self, path: str) -> bytes:
            return b"doc bytes"

        def put_bytes(self, *, key: str, data: bytes) -> str:
            self.writes[key] = data
            return f"/storage/{key}"

    class FakeParser:
        def parse_bytes(self, data: bytes, suffix: str) -> str:
            return "raw text"

    store = FakeStore()
    monkeypatch.setattr(extraction_pipeline, "get_local_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser())
    monkeypatch.setattr(extraction_pipeline, "correct_text", lambda text: CorrectionResult(text="CORRECTED"))

    result = extraction_pipeline.extract_document_from_local(path="a.txt", post_processing="clean")

    written_key = next(iter(store.writes))
    assert written_key.startswith("clean/")
    assert store.writes[written_key] == b"CORRECTED"
    assert "note" not in result


def test_extract_with_markdown_post_processing_uses_its_own_correction_call(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.writes: dict[str, bytes] = {}

        def get_bytes(self, path: str) -> bytes:
            return b"doc bytes"

        def put_bytes(self, *, key: str, data: bytes) -> str:
            self.writes[key] = data
            return f"/storage/{key}"

    class FakeParser:
        def parse_bytes(self, data: bytes, suffix: str) -> str:
            return "raw text"

    def fail_if_called(text: str) -> CorrectionResult:
        raise AssertionError("markdown mode must not call the plain-text clean correction")

    store = FakeStore()
    monkeypatch.setattr(extraction_pipeline, "get_local_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser())
    monkeypatch.setattr(extraction_pipeline, "correct_text", fail_if_called)
    monkeypatch.setattr(
        extraction_pipeline, "correct_text_to_markdown", lambda text: CorrectionResult(text="# CORRECTED MARKDOWN")
    )

    result = extraction_pipeline.extract_document_from_local(path="a.txt", post_processing="markdown")

    written_key = next(iter(store.writes))
    assert written_key.startswith("markdown/")
    assert written_key.endswith(".md")
    assert store.writes[written_key] == b"# CORRECTED MARKDOWN"
    assert result["text_path"].endswith(".md")


def test_extract_surfaces_note_when_correction_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStore:
        def get_bytes(self, path: str) -> bytes:
            return b"doc bytes"

        def put_bytes(self, *, key: str, data: bytes) -> str:
            return f"/storage/{key}"

    class FakeParser:
        def parse_bytes(self, data: bytes, suffix: str) -> str:
            return "raw text"

    monkeypatch.setattr(extraction_pipeline, "get_local_store", lambda: FakeStore())
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser())
    monkeypatch.setattr(
        extraction_pipeline,
        "correct_text",
        lambda text: CorrectionResult(text=text, note="too big to correct"),
    )

    result = extraction_pipeline.extract_document_from_local(path="a.txt", post_processing="clean")

    assert result["note"] == "too big to correct"


def test_correct_text_returns_original_for_blank_input() -> None:
    assert text_corrector.correct_text("   \n  ") == CorrectionResult(text="   \n  ")


def test_correct_text_raises_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "")

    with pytest.raises(TextCorrectionError):
        text_corrector.correct_text("some text")


def test_correct_chunk_raises_when_result_is_suspiciously_short(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMessage:
        content = "short."

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs: object) -> FakeResponse:
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(text_corrector, "_get_client", lambda: FakeClient())

    long_chunk = "word " * 200

    with pytest.raises(TextCorrectionError):
        text_corrector._correct_chunk(long_chunk, "system prompt")


def test_correct_text_falls_back_to_original_text_when_the_llm_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")

    def fail(chunk: str, system_prompt: str) -> str:
        raise TextCorrectionError("OpenAI request failed: boom")

    monkeypatch.setattr(text_corrector, "_correct_chunk", fail)

    original = "this is the original extracted text"
    result = text_corrector.correct_text(original)

    assert result.text == original
    assert result.note is not None
    assert "boom" in result.note


def test_correct_text_falls_back_to_original_text_when_one_of_several_chunks_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "max_tokens", 10)
    monkeypatch.setattr(text_corrector, "_count_tokens", lambda text: 25)
    monkeypatch.setattr(text_corrector, "_split_into_chunks", lambda text, budget: ["one", "two", "three"])

    def sometimes_fail(chunk: str, system_prompt: str) -> str:
        if chunk == "two":
            raise TextCorrectionError("OpenAI request failed: boom")
        return chunk.upper()

    monkeypatch.setattr(text_corrector, "_correct_chunk", sometimes_fail)

    original = "one two three"
    result = text_corrector.correct_text(original)

    assert result.text == original
    assert result.note is not None


def test_correct_text_skips_when_over_ten_times_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "max_tokens", 10)
    monkeypatch.setattr(text_corrector, "_count_tokens", lambda text: 101)

    def fail_if_called(chunk: str) -> str:
        raise AssertionError("OpenAI should not be called when the document is too large")

    monkeypatch.setattr(text_corrector, "_correct_chunk", fail_if_called)

    result = text_corrector.correct_text("huge document")

    assert result.text == "huge document"
    assert result.note is not None
    assert "skipped" in result.note.lower()


def test_correct_text_splits_into_parallel_chunks_and_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "max_tokens", 10)
    monkeypatch.setattr(text_corrector, "_count_tokens", lambda text: 25)
    monkeypatch.setattr(text_corrector, "_split_into_chunks", lambda text, budget: ["one", "two", "three"])
    monkeypatch.setattr(text_corrector, "_correct_chunk", lambda chunk, system_prompt: chunk.upper())

    result = text_corrector.correct_text("one two three")

    assert result.text == "ONE\n\nTWO\n\nTHREE"
    assert result.note is None


def test_clean_and_markdown_modes_use_different_system_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    seen_prompts: list[str] = []
    monkeypatch.setattr(
        text_corrector,
        "_correct_chunk",
        lambda chunk, system_prompt: seen_prompts.append(system_prompt) or chunk,
    )

    text_corrector.correct_text("hello")
    text_corrector.correct_text_to_markdown("hello")

    assert len(seen_prompts) == 2
    assert seen_prompts[0] != seen_prompts[1]
    assert "Markdown" in seen_prompts[1]


def test_split_into_chunks_respects_token_budget() -> None:
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."

    chunks = text_corrector._split_into_chunks(text, max_tokens_per_chunk=1)

    assert len(chunks) >= 2
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")


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
