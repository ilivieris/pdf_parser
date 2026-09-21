from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock

import tiktoken
from openai import OpenAI, OpenAIError

from document_processor_service.app.core.config import settings
from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.services.document_processing.exceptions import TextCorrectionError

logger = get_logger("services.document_processor_service.text_corrector")

_GUARDRAILS = (
    "Μην αλλάξεις το νόημα, μην προσθέσεις ή αφαιρέσεις πληροφορίες, αριθμούς, ημερομηνίες, "
    "ονόματα, κωδικούς (π.χ. ΑΔΑ) ή ποσά. Διατήρησε την αρχική γλώσσα του κειμένου.\n\n"
    "ΚΡΙΣΙΜΟ: το κείμενο εισόδου μπορεί να είναι πολύ μεγάλο ή να περιέχει πολλές παρόμοιες/"
    "επαναλαμβανόμενες καταχωρήσεις (π.χ. λίστες αποφάσεων, ΦΕΚ). Πρέπει να διορθώσεις και να "
    "επιστρέψεις ΟΛΟΚΛΗΡΟ το κείμενο, μέχρι το τέλος του, χωρίς καμία περίληψη, συντόμευση ή "
    "παράλειψη — ακόμα κι αν φαίνεται επαναλαμβανόμενο. Μην γράψεις ποτέ σημειώσεις τύπου "
    "'[συνεχίζεται...]', '...' ή οτιδήποτε υποδηλώνει ότι παρέλειψες μέρος του κειμένου."
)

_CLEAN_SYSTEM_PROMPT = (
    "Είσαι διορθωτής κειμένων που έχουν εξαχθεί αυτόματα από PDF/OCR. "
    "Λαμβάνεις ένα κείμενο (ή τμήμα ενός μεγαλύτερου κειμένου) που μπορεί να έχει "
    "ορθογραφικά και συντακτικά λάθη, σπασμένες προτάσεις λόγω αναδίπλωσης γραμμών, "
    "και κακή στοίχιση/μορφοποίηση.\n\n"
    "Κάνε τα εξής:\n"
    "1. Διόρθωσε ορθογραφικά και συντακτικά λάθη.\n"
    "2. Βελτίωσε τη στοίχιση και τη μορφοποίηση (παραγράφους, κενά, δομή) ώστε το κείμενο "
    "να διαβάζεται καθαρά.\n\n"
    f"{_GUARDRAILS} "
    "Επίστρεψε μόνο το διορθωμένο κείμενο, χωρίς πρόσθετα σχόλια."
)

_MARKDOWN_SYSTEM_PROMPT = (
    "Είσαι διορθωτής κειμένων που έχουν εξαχθεί αυτόματα από PDF/OCR. "
    "Λαμβάνεις ένα κείμενο (ή τμήμα ενός μεγαλύτερου κειμένου) που μπορεί να έχει "
    "ορθογραφικά και συντακτικά λάθη, σπασμένες προτάσεις λόγω αναδίπλωσης γραμμών, "
    "και κακή στοίχιση/μορφοποίηση.\n\n"
    "Κάνε τα εξής:\n"
    "1. Διόρθωσε ορθογραφικά και συντακτικά λάθη.\n"
    "2. Μορφοποίησε το αποτέλεσμα σε καθαρό Markdown: χρησιμοποίησε επικεφαλίδες (#, ##), "
    "παραγράφους, λίστες (-, 1.) και πίνακες (| ... |) όπου ταιριάζει στη δομή του πρωτότυπου εγγράφου.\n\n"
    f"{_GUARDRAILS} "
    "Επίστρεψε μόνο το Markdown κείμενο, χωρίς πρόσθετα σχόλια και χωρίς code fences (```)."
)

# Leaves headroom under settings.max_tokens for the corrected output of each chunk,
# since chat completions cap output tokens, not input tokens.
_CHUNK_SAFETY_FACTOR = 0.8

# If a correction comes back shorter than this fraction of the input, the model most likely
# summarized/truncated instead of correcting the full text — treat it as a failure rather than
# silently returning an incomplete document.
_MIN_OUTPUT_LENGTH_RATIO = 0.6


@dataclass
class CorrectionResult:
    text: str
    note: str | None = None


_client: OpenAI | None = None
_client_lock = Lock()

_encoding: tiktoken.Encoding | None = None
_encoding_lock = Lock()


def _get_client() -> OpenAI:
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is None:
            _client = OpenAI(api_key=settings.openai_api_key)
        return _client


def _get_encoding() -> tiktoken.Encoding:
    global _encoding
    if _encoding is not None:
        return _encoding

    with _encoding_lock:
        if _encoding is None:
            try:
                _encoding = tiktoken.encoding_for_model(settings.openai_model)
            except KeyError:
                _encoding = tiktoken.get_encoding("o200k_base")
        return _encoding


def _count_tokens(text: str) -> int:
    return len(_get_encoding().encode(text))


def _split_into_chunks(text: str, max_tokens_per_chunk: int) -> list[str]:
    encoding = _get_encoding()
    paragraphs = [p for p in re.split(r"\n{2,}", text) if p.strip()]

    units: list[str] = []
    for paragraph in paragraphs:
        if len(encoding.encode(paragraph)) <= max_tokens_per_chunk:
            units.append(paragraph)
            continue
        sentences = re.split(r"(?<=[.;!?])\s+", paragraph)
        units.extend(s for s in sentences if s.strip())

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = len(encoding.encode(unit))
        if current and current_tokens + unit_tokens > max_tokens_per_chunk:
            chunks.append("\n\n".join(current))
            current, current_tokens = [], 0
        current.append(unit)
        current_tokens += unit_tokens

    if current:
        chunks.append("\n\n".join(current))

    return chunks or [text]


def _correct_chunk(chunk: str, system_prompt: str) -> str:
    try:
        response = _get_client().chat.completions.create(
            model=settings.openai_model,
            max_tokens=settings.max_tokens,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": chunk},
            ],
        )
    except OpenAIError as exc:
        raise TextCorrectionError(f"OpenAI request failed: {exc}") from exc

    corrected = (response.choices[0].message.content or "").strip()
    if not corrected:
        raise TextCorrectionError("OpenAI returned an empty correction.")
    if len(corrected) < _MIN_OUTPUT_LENGTH_RATIO * len(chunk):
        raise TextCorrectionError(
            f"OpenAI returned a suspiciously short result ({len(corrected)} chars vs "
            f"{len(chunk)} chars input) — it likely summarized or truncated instead of "
            "correcting the full text."
        )
    return corrected


def _run(text: str, system_prompt: str, *, log_label: str) -> CorrectionResult:
    if not text.strip():
        return CorrectionResult(text=text)
    if not settings.openai_api_key:
        raise TextCorrectionError("OPENAI_API_KEY is not configured.")

    max_tokens = settings.max_tokens
    hard_limit = max_tokens * 10
    token_count = _count_tokens(text)

    if token_count > hard_limit:
        note = (
            f"Text correction skipped: document is ~{token_count} tokens, "
            f"which exceeds the limit of 10x MAX_TOKENS ({hard_limit})."
        )
        log_event(logger, "text_correction_skipped", mode=log_label, token_count=token_count, limit=hard_limit)
        return CorrectionResult(text=text, note=note)

    if token_count <= max_tokens:
        chunks = [text]
    else:
        chunk_budget = max(1, int(max_tokens * _CHUNK_SAFETY_FACTOR))
        chunks = _split_into_chunks(text, chunk_budget)

    log_event(logger, "text_correction_started", mode=log_label, token_count=token_count, chunk_count=len(chunks))

    try:
        if len(chunks) == 1:
            corrected_chunks = [_correct_chunk(chunks[0], system_prompt)]
        else:
            workers = min(len(chunks), max(1, settings.document_correction_chunk_parallelism))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                corrected_chunks = list(pool.map(lambda c: _correct_chunk(c, system_prompt), chunks))
    except TextCorrectionError as exc:
        note = f"Text correction failed, returning the original extracted text instead: {exc}"
        log_event(
            logger,
            "text_correction_failed",
            mode=log_label,
            token_count=token_count,
            chunk_count=len(chunks),
            error=str(exc),
        )
        return CorrectionResult(text=text, note=note)

    corrected_text = "\n\n".join(corrected_chunks)
    log_event(logger, "text_correction_finished", mode=log_label, chunk_count=len(chunks), char_count=len(corrected_text))
    return CorrectionResult(text=corrected_text)


def correct_text(text: str) -> CorrectionResult:
    """Fix spelling/grammar and layout, returning plain text."""
    return _run(text, _CLEAN_SYSTEM_PROMPT, log_label="clean")


def correct_text_to_markdown(text: str) -> CorrectionResult:
    """Fix spelling/grammar and render the result as Markdown, in a single pass."""
    return _run(text, _MARKDOWN_SYSTEM_PROMPT, log_label="markdown")
