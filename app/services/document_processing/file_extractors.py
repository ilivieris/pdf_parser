from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from document_processor_service.app.core.config import settings
from document_processor_service.app.core.logging import get_logger
from document_processor_service.app.services.document_processing.exceptions import ExtractionError
from document_processor_service.app.services.document_processing.text_normalizer import normalize_extracted_text

logger = get_logger("services.document_processor_service.extractors")

_IRREGULAR_CHARS = {
    "\u00A0": " ",
    "\u2002": " ",
    "\u2003": " ",
    "\u2009": " ",
    "\u202F": " ",
    "\u00AD": "",
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201A": "'",
    "\u201B": "'",
    "\u201C": '"',
    "\u201D": '"',
    "\u201E": '"',
    "\u201F": '"',
    "\u2022": "-",
    "\u2032": "'",
    "\u2033": '"',
    "\u00C3\u00A2\u00E2\u201A\u00AC\u00C2\u00A2": "-",
    "\u00C3\u00A2\u00E2\u201A\u00AC\u00E2\u20AC\u0153": "-",
    "\u00C3\u00A2\u00E2\u201A\u00AC\u00E2\u20AC\u009D": "-",
    "\u00C3\u00A2\u00E2\u201A\u00AC\u00CB\u0153": "'",
    "\u00C3\u00A2\u00E2\u201A\u00AC\u00E2\u201E\u00A2": "'",
    "\u00C3\u00A2\u00E2\u201A\u00AC\u00C5\u201C": '"',
    "\u00C3\u00A2\u00E2\u201A\u00AC\u00EF\u00BF\u00BD": '"',
}

def parse_pdf(path: Path) -> str:
    import pdfplumber

    raw_pages_text: list[str] = []
    rendered_pages_text: list[str] = []

    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            raw_text = page.extract_text() or ""

            if raw_text.strip():
                raw_pages_text.append(raw_text)

            tables = _find_tables(page)
            if tables:
                rendered_page = _render_page_with_tables(page, tables)
            else:
                rendered_page = _preprocess_pdf_text(raw_text)

            if rendered_page.strip():
                rendered_pages_text.append(rendered_page.strip())

    if not raw_pages_text:
        return _parse_pdf_via_ocr_or_raise(path)

    raw_text = "\n\n".join(raw_pages_text)
    if _has_cid_garbage(raw_text):
        ocr_text = _parse_pdf_ocr(path)
        if ocr_text.strip():
            return ocr_text

    if rendered_pages_text:
        return "\n\n".join(rendered_pages_text).strip()

    return _parse_pdf_via_ocr_or_raise(path)


def parse_docx(path: Path) -> str:
    import mammoth

    with open(path, "rb") as handle:
        result = mammoth.extract_raw_text(handle)
    return normalize_extracted_text(result.value or "")


def parse_doc(path: Path) -> str:
    temp_dir = Path(tempfile.mkdtemp())
    libreoffice_profile = temp_dir / f"lo-{uuid.uuid4().hex}"
    try:
        result = subprocess.run(
            [
                "soffice",
                "--headless",
                f"-env:UserInstallation={libreoffice_profile.as_uri()}",
                "--convert-to",
                "docx",
                "--outdir",
                str(temp_dir),
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise ExtractionError(f"DOC conversion failed for '{path.name}': {details or 'unknown error'}")

        docx_path = temp_dir / f"{path.stem}.docx"
        if not docx_path.exists():
            raise ExtractionError(f"DOC conversion did not produce a .docx file for '{path.name}'")

        extracted_text = parse_docx(docx_path)
        if extracted_text.strip():
            return extracted_text
        raise ExtractionError(f"No text could be extracted from converted DOC file '{path.name}'")
    except FileNotFoundError as exc:
        raise ExtractionError("DOC extraction requires LibreOffice (soffice) in the runtime image.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"DOC conversion timed out for '{path.name}'") from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def parse_plain_text(path: Path) -> str:
    return normalize_extracted_text(path.read_text(encoding="utf-8", errors="replace"))


def _has_cid_garbage(text: str, threshold: float = 0.05) -> bool:
    cid_hits = len(re.findall(r"\(cid:\d+\)", text))
    if cid_hits == 0:
        return False
    total_tokens = max(len(text.split()), 1)
    return (cid_hits / total_tokens) > threshold


def _parse_pdf_ocr(path: Path) -> str:
    try:
        import pdf2image  # type: ignore
        import pytesseract  # type: ignore
    except ImportError:
        return ""

    try:
        pages_text: list[str] = []
        deadline = time.monotonic() + settings.document_ocr_timeout_seconds
        try:
            info = pdf2image.pdfinfo_from_path(str(path))
            page_count = int(info.get("Pages", 0))
        except Exception:
            logger.warning("Could not determine PDF page count before OCR for %s", path.name)
            page_count = settings.document_ocr_max_pages

        page_limit = min(max(page_count, 0) or settings.document_ocr_max_pages, settings.document_ocr_max_pages)
        if page_count > settings.document_ocr_max_pages:
            logger.warning(
                "PDF OCR page cap applied for %s: processing %s of %s pages",
                path.name,
                page_limit,
                page_count,
            )

        for page_number in range(1, page_limit + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("PDF OCR time budget exhausted for %s", path.name)
                break

            try:
                images = pdf2image.convert_from_path(
                    str(path),
                    dpi=settings.document_ocr_dpi,
                    first_page=page_number,
                    last_page=page_number,
                    timeout=max(1, int(remaining)),
                )
                for image in images:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning("PDF OCR time budget exhausted for %s", path.name)
                        break
                    page_text = pytesseract.image_to_string(image, lang="ell+eng", timeout=max(1, int(remaining)))
                    if page_text.strip():
                        pages_text.append(page_text)
            except Exception:
                logger.exception("PDF OCR failed on page %s for %s", page_number, path.name)
                break

        return normalize_extracted_text("\n\n".join(pages_text), join_line_wrapped=True)
    except Exception:
        logger.exception("PDF OCR extraction failed for %s", path.name)
        return ""


def _parse_pdf_via_ocr_or_raise(path: Path) -> str:
    ocr_text = _parse_pdf_ocr(path)
    if ocr_text.strip():
        return ocr_text
    raise ExtractionError(f"No text could be extracted from PDF '{path.name}'")


def _replace_irregular_chars(text: str) -> str:
    for char, replacement in _IRREGULAR_CHARS.items():
        text = text.replace(char, replacement)
    return text


def _normalize_layout_line(line: str) -> str:
    normalized = _replace_irregular_chars(line.rstrip())
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _escape_markdown_cell(value: str) -> str:
    return value.replace("|", "\\|")


# Multi-column, newspaper-style body text (very common in ΦΕΚ/Diavgeia PDFs) produces the
# exact same strong, repeated column alignment as a real table — one "row" per line of prose,
# for the whole page. Genuine borderless label/value tables stay well under this in practice.
_MAX_TEXT_STRATEGY_TABLE_ROWS = 20


def _find_tables(page):
    # Bordered tables first (ruling lines); Diavgeia-style documents are often
    # borderless, so fall back to alignment-based detection when that finds nothing.
    # min_words_vertical is set high to require strong, repeated column alignment —
    # otherwise ordinary justified paragraphs get misdetected as tables and words
    # get split mid-word at the perceived column boundaries.
    tables = page.find_tables()
    if tables:
        return tables

    candidates = page.find_tables(
        table_settings={"vertical_strategy": "text", "horizontal_strategy": "text", "min_words_vertical": 20}
    )
    return [t for t in candidates if len(t.rows) <= _MAX_TEXT_STRATEGY_TABLE_ROWS]


def _table_to_markdown(rows: list[list[str | None]]) -> str:
    cleaned_rows = [
        [_normalize_layout_line((cell or "").replace("\n", " ")) for cell in row]
        for row in rows
        if any((cell or "").strip() for cell in row)
    ]
    if not cleaned_rows:
        return ""

    width = max(len(row) for row in cleaned_rows)
    cleaned_rows = [row + [""] * (width - len(row)) for row in cleaned_rows]

    header, *data_rows = cleaned_rows
    lines = [
        "| " + " | ".join(_escape_markdown_cell(cell) for cell in header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in data_rows:
        lines.append("| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |")

    return "\n".join(lines)


def _group_words_into_lines(words: list[dict], tolerance: float = 3.0) -> list[tuple[float, str]]:
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    grouped: list[list[dict]] = []
    for word in ordered:
        if grouped and abs(word["top"] - grouped[-1][-1]["top"]) <= tolerance:
            grouped[-1].append(word)
        else:
            grouped.append([word])

    lines: list[tuple[float, str]] = []
    for line_words in grouped:
        line_words.sort(key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in line_words)
        lines.append((min(w["top"] for w in line_words), text))
    return lines


def _render_page_with_tables(page, tables) -> str:
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    lines = _group_words_into_lines(words)

    # (vertical position, kind, content) so text and tables interleave in reading order.
    blocks: list[tuple[float, str, str]] = []
    for top, text in lines:
        if any(table.bbox[1] - 1 <= top <= table.bbox[3] + 1 for table in tables):
            continue
        normalized = _normalize_layout_line(text)
        if normalized:
            blocks.append((top, "text", normalized))

    for table in tables:
        markdown = _table_to_markdown(table.extract())
        if markdown:
            blocks.append((table.bbox[1], "table", markdown))

    blocks.sort(key=lambda block: block[0])

    sections: list[str] = []
    text_buffer: list[str] = []

    def flush_text() -> None:
        nonlocal text_buffer
        if not text_buffer:
            return
        # Each item is already one reconstructed visual line, so only join
        # hyphen-wrapped words across line breaks — don't collapse lines into a
        # single paragraph the way _preprocess_pdf_text does for raw PDF text.
        block = _fix_pdf_hyphenation("\n".join(text_buffer)).strip()
        if block:
            sections.append(block)
        text_buffer = []

    for _, kind, content in blocks:
        if kind == "table":
            flush_text()
            sections.append(content)
        else:
            text_buffer.append(content)

    flush_text()
    return "\n\n".join(section for section in sections if section.strip())


def _fix_pdf_hyphenation(text: str) -> str:
    return re.sub(r"-\s*\n(?!\n)\s*([a-z\u03b1-\u03c9])", r"\1", text)


def _preprocess_pdf_text(text: str) -> str:
    text = _fix_pdf_hyphenation(text)
    text = re.sub(r",\s*\n+\s*", ", ", text)
    text = re.sub(r"\.\n(?!\n)", ". ", text)
    text = re.sub(r"-\n(?!\n)", "", text)
    text = re.sub(r"\n(?!\n)", " ", text)
    text = text.replace("\x07", "")
    text = re.sub(r"\xa0", " ", text)
    text = re.sub(r" +", " ", text)
    text = re.sub(r"\.+", ".", text)
    text = _replace_irregular_chars(text)
    return text.strip()
