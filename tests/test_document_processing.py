from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from document_processor_service.app.core import object_storage, service_clients
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


def _find_diavgeia_sample(name: str) -> Path | None:
    root = Path(__file__).resolve().parents[1] / "diavgeia_sample"
    if not root.is_dir():
        return None
    return next((p for p in root.rglob(name)), None)


def test_multi_column_gazette_pages_are_not_misdetected_as_tables() -> None:
    sample_pdf = _find_diavgeia_sample("1.pdf")
    if sample_pdf is None:
        pytest.skip("sample file '1.pdf' not present under diavgeia_sample")

    parsed = parse_pdf(sample_pdf)

    # This document is plain two-column gazette body text with no real tables. The strong,
    # repeated column alignment of a two-column layout must not be misdetected as a table
    # (which would otherwise split words mid-word at the perceived column boundaries).
    assert "| --- |" not in parsed
    assert "ΑΡΙΣΤΟΤΕΛΕΙΟ ΠΑΝΕΠΙΣΤΗΜΙΟ" in parsed


def test_bordered_table_is_still_detected_next_to_gazette_style_pages() -> None:
    sample_pdf = _find_diavgeia_sample("2.pdf")
    if sample_pdf is None:
        pytest.skip("sample file '2.pdf' not present under diavgeia_sample")

    parsed = parse_pdf(sample_pdf)

    assert "| --- |" in parsed
    assert "| 58206" in parsed
    assert "80.849,81" in parsed


def test_parse_bytes_rejects_empty_suffix() -> None:
    parser = DocumentParser(supported_extensions=[".txt"])

    with pytest.raises(UnsupportedFormatError):
        parser.parse_bytes(b"hello", "")


class FakeStore:
    """Stands in for MinioObjectStore: records writes, serves back whatever was put."""

    bucket = "test-bucket"

    def __init__(self) -> None:
        self.writes: dict[str, bytes] = {}
        self.removed: list[str] = []
        self.retention: dict[str, int] | None = None

    def put_bytes(self, *, key: str, data: bytes, content_type: str = "") -> str:
        self.writes[key] = data
        return key

    def get_bytes(self, key: str) -> bytes:
        return self.writes[key]

    def remove_object(self, key: str) -> None:
        self.removed.append(key)
        self.writes.pop(key, None)

    def presigned_url(self, key: str, *, expires_seconds: int | None = None) -> str:
        return f"http://minio.test/{self.bucket}/{key}?signed=1"

    def apply_retention_policy(self, rules: dict[str, int]) -> None:
        self.retention = rules

    def ping(self) -> None:
        return None

    # -- helpers for assertions -------------------------------------------------
    def key_under(self, prefix: str) -> str:
        return next(key for key in self.writes if key.startswith(f"{prefix}/"))


class FakeParser:
    def __init__(self, text: str = "extracted text") -> None:
        self.text = text
        self.suffixes: list[str] = []

    def parse_bytes(self, data: bytes, suffix: str) -> str:
        self.suffixes.append(suffix)
        return self.text


def test_extract_uses_filename_suffix_to_pick_the_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = FakeParser()
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: FakeStore())
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: parser)

    result = extraction_pipeline.extract_document_from_upload(filename="source.doc", data=b"doc bytes")

    assert parser.suffixes == [".doc"]
    assert result["filename"] == "source.doc"


def test_extract_uploads_the_source_then_parses_what_the_bucket_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bucket, not the request body, is what gets parsed."""

    class ReadBackStore(FakeStore):
        def get_bytes(self, key: str) -> bytes:
            return b"BYTES FROM BUCKET"

    seen: list[bytes] = []

    class RecordingParser(FakeParser):
        def parse_bytes(self, data: bytes, suffix: str) -> str:
            seen.append(data)
            return super().parse_bytes(data, suffix)

    store = ReadBackStore()
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: RecordingParser())

    extraction_pipeline.extract_document_from_upload(filename="a.pdf", data=b"uploaded bytes")

    assert store.writes[store.key_under(settings.minio_source_prefix)] == b"uploaded bytes"
    assert seen == [b"BYTES FROM BUCKET"]


def test_response_carries_only_the_filename_and_the_download_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Object keys are an internal layout detail and stay out of the contract."""
    store = FakeStore()
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser())

    result = extraction_pipeline.extract_document_from_upload(filename="r45.pdf", data=b"doc bytes")

    assert set(result) == {"filename", "download_url"}
    output_key = store.key_under(settings.minio_output_prefix)
    assert result["download_url"] == f"http://minio.test/test-bucket/{output_key}?signed=1"


def test_source_object_keeps_the_uploaded_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore()
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser())

    extraction_pipeline.extract_document_from_upload(filename="r45.pdf", data=b"doc bytes")

    assert store.key_under(settings.minio_source_prefix).endswith("/r45.pdf")


def test_non_ascii_filenames_are_sanitized_into_the_object_key(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore()
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser())

    extraction_pipeline.extract_document_from_upload(
        filename="../../ΑΠΟΦΑΣΗ 12/3.pdf", data=b"doc bytes"
    )

    # Directory components are stripped and the name is collapsed to key-safe characters.
    source_key = store.key_under(settings.minio_source_prefix)
    assert source_key.endswith("/3.pdf")
    assert ".." not in source_key


def test_extract_rejects_an_empty_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: FakeStore())

    with pytest.raises(ValueError):
        extraction_pipeline.extract_document_from_upload(filename="a.pdf", data=b"")


@pytest.mark.parametrize(
    ("post_processing", "expected_suffix"),
    [("none", ".txt"), ("clean", "_clean.txt"), ("markdown", ".md")],
)
def test_output_key_is_flat_artifact_id_with_mode_specific_suffix(
    monkeypatch: pytest.MonkeyPatch, post_processing: str, expected_suffix: str
) -> None:
    store = FakeStore()
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser("text"))
    monkeypatch.setattr(extraction_pipeline, "correct_text", lambda text: CorrectionResult(text=text))
    monkeypatch.setattr(extraction_pipeline, "correct_text_to_markdown", lambda text: CorrectionResult(text=text))

    extraction_pipeline.extract_document_from_upload(
        filename="r45.pdf", data=b"doc bytes", post_processing=post_processing
    )

    output_key = store.key_under(settings.minio_output_prefix)
    assert output_key.endswith(expected_suffix)
    # One flat level under the output prefix -- no nested subfolders.
    assert output_key.count("/") == 1


def test_extract_with_clean_post_processing_writes_corrected_output(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore()
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser("raw text"))
    monkeypatch.setattr(extraction_pipeline, "correct_text", lambda text: CorrectionResult(text="CORRECTED"))

    result = extraction_pipeline.extract_document_from_upload(
        filename="a.txt", data=b"doc bytes", post_processing="clean"
    )

    output_key = store.key_under(settings.minio_output_prefix)
    assert output_key.endswith("_clean.txt")
    assert store.writes[output_key] == b"CORRECTED"
    assert "note" not in result


def test_extract_with_markdown_post_processing_uses_its_own_correction_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called(text: str) -> CorrectionResult:
        raise AssertionError("markdown mode must not call the plain-text clean correction")

    store = FakeStore()
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser("raw text"))
    monkeypatch.setattr(extraction_pipeline, "correct_text", fail_if_called)
    monkeypatch.setattr(
        extraction_pipeline, "correct_text_to_markdown", lambda text: CorrectionResult(text="# CORRECTED MARKDOWN")
    )

    extraction_pipeline.extract_document_from_upload(
        filename="a.txt", data=b"doc bytes", post_processing="markdown"
    )

    output_key = store.key_under(settings.minio_output_prefix)
    assert output_key.endswith(".md")
    assert store.writes[output_key] == b"# CORRECTED MARKDOWN"


def test_extract_surfaces_note_when_correction_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: FakeStore())
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser("raw text"))
    monkeypatch.setattr(
        extraction_pipeline,
        "correct_text",
        lambda text: CorrectionResult(text=text, note="too big to correct"),
    )

    result = extraction_pipeline.extract_document_from_upload(
        filename="a.txt", data=b"doc bytes", post_processing="clean"
    )

    assert result["note"] == "too big to correct"


def test_source_is_kept_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore()
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser())
    monkeypatch.setattr(settings, "minio_delete_source_after_extract", False)

    extraction_pipeline.extract_document_from_upload(filename="a.pdf", data=b"doc bytes")

    assert store.removed == []


def test_source_is_deleted_after_extraction_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore()
    monkeypatch.setattr(extraction_pipeline, "get_object_store", lambda: store)
    monkeypatch.setattr(extraction_pipeline, "DocumentParser", lambda: FakeParser())
    monkeypatch.setattr(settings, "minio_delete_source_after_extract", True)

    result = extraction_pipeline.extract_document_from_upload(filename="a.pdf", data=b"doc bytes")

    assert len(store.removed) == 1
    assert store.removed[0].startswith(f"{settings.minio_source_prefix}/")
    # The output and its link survive -- only the source is dropped.
    assert store.key_under(settings.minio_output_prefix) in result["download_url"]


def test_correct_text_returns_original_for_blank_input() -> None:
    assert text_corrector.correct_text("   \n  ") == CorrectionResult(text="   \n  ")


def test_correct_text_raises_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "")

    with pytest.raises(TextCorrectionError):
        text_corrector.correct_text("some text")


def test_correct_chunk_sends_one_request_and_returns_the_model_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    class FakeMessage:
        content = "  corrected text  "

    class FakeChoice:
        message = FakeMessage()
        finish_reason = "stop"

    class FakeUsage:
        prompt_tokens = 10
        completion_tokens = 5

    class FakeResponse:
        choices = [FakeChoice()]
        usage = FakeUsage()

    class FakeCompletions:
        def create(self, **kwargs: object) -> FakeResponse:
            calls.append(kwargs)
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(text_corrector, "_get_client", lambda: FakeClient())

    result = text_corrector._correct_chunk("some input text", "system prompt")

    assert result == "corrected text"
    assert len(calls) == 1  # no retries


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
    monkeypatch.setattr(settings, "document_chunk_tokens", 10)
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
    monkeypatch.setattr(settings, "document_chunk_tokens", 10)
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


def test_split_into_chunks_fills_chunks_up_to_the_budget() -> None:
    """Paragraphs are packed, not sent one per chunk.

    A budget that fits several paragraphs must produce correspondingly fewer chunks --
    otherwise a large document explodes into hundreds of needless OpenAI calls.
    """
    paragraph = "Παράγραφος με αρκετό κείμενο ώστε να μετρηθεί σε tokens."
    text = "\n\n".join([paragraph] * 40)

    small = text_corrector._split_into_chunks(text, max_tokens_per_chunk=40)
    large = text_corrector._split_into_chunks(text, max_tokens_per_chunk=200)

    assert len(large) < len(small)
    for chunk in large:
        assert text_corrector._count_tokens(chunk) <= 200
    assert "".join(large).replace("\n\n", "") == text.replace("\n\n", "")


def test_default_chunk_budget_stays_below_the_measured_safe_ceiling() -> None:
    """gpt-4.1 silently drops content at 14000+ input tokens per chunk (see README).

    Raising the default past 12000 reintroduces that failure, and it is invisible at
    runtime -- finish_reason stays "stop" and no placeholder is emitted -- so guard the
    default here instead.
    """
    assert 0 < settings.document_chunk_tokens <= 12000


def test_extract_endpoint_sanitizes_extraction_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from document_processor_service.app import main

    def fail_extract(**kwargs: object) -> dict[str, object]:
        raise ExtractionError("leaky stderr /tmp/private/path")

    monkeypatch.setattr(main, "extract_document_from_upload", fail_extract)
    response = TestClient(main.app).post(
        "/extract",
        files={"file": ("file.pdf", b"%PDF-1.4", "application/pdf")},
        data={"post_processing": "none"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Document text extraction failed."


def test_extract_endpoint_rejects_uploads_over_the_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from document_processor_service.app import main

    def must_not_run(**kwargs: object) -> dict[str, object]:
        raise AssertionError("an oversized upload must be rejected before it reaches MinIO")

    monkeypatch.setattr(main, "extract_document_from_upload", must_not_run)
    monkeypatch.setattr(settings, "max_upload_size_bytes", 8)
    response = TestClient(main.app).post(
        "/extract",
        files={"file": ("file.pdf", b"x" * 64, "application/pdf")},
    )

    assert response.status_code == 413


@pytest.mark.parametrize(
    ("endpoint", "default_secure", "expected"),
    [
        ("minio:9000", False, ("minio:9000", False)),
        ("http://minio:9000", True, ("minio:9000", False)),
        ("https://storage.example.com", False, ("storage.example.com", True)),
        ("  localhost:9000  ", False, ("localhost:9000", False)),
    ],
)
def test_split_endpoint_normalizes_host_and_tls_flag(
    endpoint: str, default_secure: bool, expected: tuple[str, bool]
) -> None:
    assert object_storage.split_endpoint(endpoint, default_secure=default_secure) == expected


def test_public_endpoint_gets_its_own_presigning_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """SigV4 signs the Host header, so a public link must be signed by a client bound to that host.

    Rewriting the host on an already-signed URL yields SignatureDoesNotMatch.
    """
    built: list[tuple[str, bool]] = []

    class FakeMinio:
        def __init__(self, endpoint: str, access_key=None, secret_key=None, secure=False, region=None) -> None:
            built.append((endpoint, secure))

        def bucket_exists(self, bucket: str) -> bool:
            return True

    monkeypatch.setattr(object_storage, "Minio", FakeMinio)
    monkeypatch.setattr(settings, "minio_endpoint", "minio:9000")
    monkeypatch.setattr(settings, "minio_public_endpoint", "localhost:9000")
    monkeypatch.setattr(settings, "minio_secure", False)

    store = object_storage.MinioObjectStore()

    assert built == [("minio:9000", False), ("localhost:9000", False)]
    assert store._presign_client is not store._client


def test_no_public_endpoint_reuses_the_single_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMinio:
        def __init__(self, endpoint: str, access_key=None, secret_key=None, secure=False, region=None) -> None:
            pass

        def bucket_exists(self, bucket: str) -> bool:
            return True

    monkeypatch.setattr(object_storage, "Minio", FakeMinio)
    monkeypatch.setattr(settings, "minio_endpoint", "minio:9000")
    monkeypatch.setattr(settings, "minio_public_endpoint", "")

    store = object_storage.MinioObjectStore()

    assert store._presign_client is store._client


def main_module():
    from document_processor_service.app import main

    return main


def test_health_reports_every_check_and_returns_200_when_they_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from document_processor_service.app import main

    class LiveStore:
        def ping(self) -> None:
            return None

    monkeypatch.setattr(service_clients, "get_object_store", lambda: LiveStore())
    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "document_processor_service"
    assert body["version"]
    assert body["uptime_seconds"] >= 0
    assert [check["status"] for check in body["checks"]] == ["pass"]
    assert body["checks"][0]["latency_ms"] >= 0


def test_health_returns_503_and_names_the_failure_when_minio_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable():
        raise object_storage.ObjectStorageError("connection refused")

    monkeypatch.setattr(service_clients, "get_object_store", unreachable)
    response = TestClient(main_module().app).get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    check = body["checks"][0]
    assert check["status"] == "fail"
    assert "connection refused" in check["detail"]
    # The endpoint still answers with a full report rather than raising.
    assert check["name"].startswith("minio:")


def test_health_check_does_not_propagate_unmapped_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """An SDK error the storage layer does not wrap must still produce a report, not a 500."""

    def boom():
        raise TimeoutError("read timed out")

    monkeypatch.setattr(service_clients, "get_object_store", boom)
    check = service_clients.check_object_storage()

    assert check.status == "fail"
    assert "TimeoutError" in (check.detail or "")


def test_liveness_is_dependency_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """A MinIO outage must not fail liveness, or the container gets restarted into a crash loop."""

    def must_not_be_called():
        raise AssertionError("liveness must not probe MinIO")

    monkeypatch.setattr(service_clients, "get_object_store", must_not_be_called)
    response = TestClient(main_module().app).get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_retention_rules_only_include_prefixes_with_a_configured_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "minio_source_prefix", "sources")
    monkeypatch.setattr(settings, "minio_output_prefix", "extracted")
    monkeypatch.setattr(settings, "minio_source_retention_days", 3)
    monkeypatch.setattr(settings, "minio_output_retention_days", 0)

    assert service_clients.retention_rules() == {"sources": 3}


def test_retention_rules_are_empty_when_nothing_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "minio_source_retention_days", 0)
    monkeypatch.setattr(settings, "minio_output_retention_days", 0)

    assert service_clients.retention_rules() == {}


def test_retention_policy_replaces_our_rules_and_keeps_foreign_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bucket shared with another workload must keep the rules this service did not write."""
    from minio.commonconfig import ENABLED, Filter
    from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

    foreign = Rule(
        ENABLED,
        rule_filter=Filter(prefix="somebody-elses/"),
        rule_id="their-rule",
        expiration=Expiration(days=90),
    )
    stale_ours = Rule(
        ENABLED,
        rule_filter=Filter(prefix="sources/"),
        rule_id="document-processor-sources",
        expiration=Expiration(days=999),
    )
    applied: list[LifecycleConfig] = []

    class FakeClient:
        def get_bucket_lifecycle(self, bucket: str) -> LifecycleConfig:
            return LifecycleConfig([foreign, stale_ours])

        def set_bucket_lifecycle(self, bucket: str, config: LifecycleConfig) -> None:
            applied.append(config)

    store = object.__new__(object_storage.MinioObjectStore)
    store._bucket = "documents"
    store._client = FakeClient()

    store.apply_retention_policy({"sources": 7})

    rule_ids = [rule.rule_id for rule in applied[0].rules]
    assert "their-rule" in rule_ids
    assert rule_ids.count("document-processor-sources") == 1
    ours = next(r for r in applied[0].rules if r.rule_id == "document-processor-sources")
    assert ours.expiration.days == 7


def test_remove_object_treats_a_missing_object_as_success() -> None:
    from minio.error import S3Error

    class FakeClient:
        def remove_object(self, bucket: str, key: str) -> None:
            raise S3Error(None, "NoSuchKey", "gone", key, "req", "host")

    store = object.__new__(object_storage.MinioObjectStore)
    store._bucket = "documents"
    store._client = FakeClient()

    store.remove_object("sources/abc/1.pdf")  # must not raise


# ---------------------------------------------------------------------------
# /analyze -- upload-based, parsed in memory, nothing stored
# ---------------------------------------------------------------------------


def _stub_semantic_calls(monkeypatch: pytest.MonkeyPatch, text: str = "κείμενο") -> None:
    """Neutralises the network-bound halves of the analyse pipeline."""
    from document_processor_service.app.services.semantic_analysis import pipeline

    monkeypatch.setattr(pipeline, "DocumentParser", lambda: FakeParser(text))
    monkeypatch.setattr(pipeline, "extract_cpv", lambda t: [])
    monkeypatch.setattr(pipeline, "extract_budget_codes", lambda t: [])
    monkeypatch.setattr(
        pipeline,
        "extract_llm_fields",
        lambda t: {"decision_type": None, "cpv": [], "budget_codes": []},
    )
    monkeypatch.setattr(pipeline, "extract_skills", lambda t: [])


def test_analyze_parses_the_uploaded_bytes_without_touching_the_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from document_processor_service.app.services.semantic_analysis import pipeline

    seen: list[tuple[bytes, str]] = []

    class RecordingParser(FakeParser):
        def parse_bytes(self, data: bytes, suffix: str) -> str:
            seen.append((data, suffix))
            return super().parse_bytes(data, suffix)

    _stub_semantic_calls(monkeypatch)
    monkeypatch.setattr(pipeline, "DocumentParser", lambda: RecordingParser("κείμενο"))

    # Any real file read would blow up here, since this name exists nowhere.
    result = pipeline.analyze_document_from_upload(filename="ουδέποτε-υπήρξε.txt", data=b"raw bytes")

    assert seen == [(b"raw bytes", ".txt")]
    assert result["filename"] == "ουδέποτε-υπήρξε.txt"


def test_analyze_response_no_longer_carries_a_source_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing is stored, so there is no path to point at."""
    from document_processor_service.app.services.semantic_analysis import pipeline

    _stub_semantic_calls(monkeypatch)
    result = pipeline.analyze_document_from_upload(filename="a.txt", data=b"bytes")

    assert "source_path" not in result
    assert set(result) == {
        "filename",
        "artifact_id",
        "decision_type",
        "cpv",
        "budget_codes",
        "skills",
        "extraction_note",
        "skills_note",
    }


def test_analyze_artifact_id_is_stable_for_identical_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    from document_processor_service.app.services.semantic_analysis import pipeline

    _stub_semantic_calls(monkeypatch)
    first = pipeline.analyze_document_from_upload(filename="a.txt", data=b"same bytes")
    second = pipeline.analyze_document_from_upload(filename="a.txt", data=b"same bytes")
    other = pipeline.analyze_document_from_upload(filename="a.txt", data=b"other bytes")

    assert first["artifact_id"] == second["artifact_id"]
    assert first["artifact_id"] != other["artifact_id"]


@pytest.mark.parametrize(
    ("filename", "data"),
    [("a.txt", b""), ("", b"bytes")],
)
def test_analyze_rejects_an_empty_upload_or_a_missing_filename(
    monkeypatch: pytest.MonkeyPatch, filename: str, data: bytes
) -> None:
    from document_processor_service.app.services.semantic_analysis import pipeline

    _stub_semantic_calls(monkeypatch)

    with pytest.raises(ValueError):
        pipeline.analyze_document_from_upload(filename=filename, data=data)


def test_analyze_endpoint_accepts_multipart_and_returns_the_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from document_processor_service.app.services.semantic_analysis import router

    def fake_analyze(*, filename: str, data: bytes) -> dict:
        return {
            "filename": filename,
            "artifact_id": "deadbeefdeadbeef",
            "decision_type": None,
            "cpv": [],
            "budget_codes": [],
            "skills": [],
            "extraction_note": None,
            "skills_note": None,
        }

    monkeypatch.setattr(router, "analyze_document_from_upload", fake_analyze)
    response = TestClient(main_module().app).post(
        "/analyze",
        files={"file": ("1_clean.txt", "κείμενο".encode("utf-8"), "text/plain")},
    )

    assert response.status_code == 200
    assert response.json()["filename"] == "1_clean.txt"


def test_analyze_endpoint_rejects_uploads_over_the_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from document_processor_service.app.services.semantic_analysis import router

    def must_not_run(**kwargs: object) -> dict:
        raise AssertionError("an oversized upload must be rejected before it is parsed")

    monkeypatch.setattr(router, "analyze_document_from_upload", must_not_run)
    monkeypatch.setattr(settings, "max_upload_size_bytes", 8)
    response = TestClient(main_module().app).post(
        "/analyze",
        files={"file": ("big.txt", b"x" * 64, "text/plain")},
    )

    assert response.status_code == 413
