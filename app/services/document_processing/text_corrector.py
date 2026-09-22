from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock

import tiktoken
from openai import OpenAI, OpenAIError

from document_processor_service.app.core.config import settings
from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.services.document_processing.exceptions import TextCorrectionError

logger = get_logger("services.document_processor_service.text_corrector")

# The input is always presented as a complete, self-contained document. Telling the model it is
# looking at "part of a larger text" is what invites it to answer with a "the rest continues..."
# note instead of the actual content. The output contract anchors both ends of the output to the
# input, which is what actually stops the model from trailing off mid-document.
_ROLE = (
    "Είσαι συνάρτηση μετασχηματισμού κειμένου, όχι συνομιλητής. Η έξοδός σου είναι το κείμενο της "
    "εισόδου διορθωμένο — τίποτα άλλο.\n\n"
    "Το κείμενο που λαμβάνεις είναι αυτοτελές και ολοκληρωμένο. Δεν υπάρχει άλλο κείμενο πριν ή "
    "μετά από αυτό, και δεν πρόκειται να σου ζητηθεί συνέχεια. Η έξοδός σου γράφεται αυτόματα σε "
    "αρχείο που παραδίδεται ως επίσημο έγγραφο, χωρίς να τη δει άνθρωπος ενδιάμεσα."
)

_OUTPUT_CONTRACT = (
    "ΣΥΜΒΟΛΑΙΟ ΕΞΟΔΟΥ — ισχύει χωρίς εξαίρεση:\n"
    "1. Η πρώτη λέξη της εξόδου σου είναι η πρώτη λέξη της εισόδου, διορθωμένη.\n"
    "2. Η τελευταία λέξη της εξόδου σου είναι η τελευταία λέξη της εισόδου, διορθωμένη.\n"
    "3. Ανάμεσά τους υπάρχει κάθε πρόταση, παράγραφος, άρθρο και καταχώρηση της εισόδου, με την "
    "ίδια σειρά. Τα διοικητικά κείμενα έχουν πολλές σχεδόν πανομοιότυπες καταχωρήσεις· καμία δεν "
    "συγχωνεύεται, καμία δεν συντομεύεται, καμία δεν παραλείπεται.\n"
    "4. Δεν γράφεις ΠΟΤΕ κείμενο που περιγράφει τι έκανες, τι ακολουθεί ή τι παρέλειψες. "
    "Απαγορεύονται ρητά: φράσεις μέσα σε αγκύλες [ ], σχόλια, εισαγωγή, επίλογος, περίληψη, "
    "αποσιωπητικά που αντικαθιστούν περιεχόμενο, οδηγίες προς τον αναγνώστη.\n"
    "5. Αν πιάσεις τον εαυτό σου να περιγράφει τη διαδικασία αντί να την εκτελεί, σταμάτα και "
    "γράψε το πραγματικό κείμενο της εισόδου."
)

_CONSTRAINTS = (
    "ΔΕΝ ΑΛΛΑΖΕΙΣ: το νόημα, τους αριθμούς, τις ημερομηνίες, τα ονόματα, τους κωδικούς (π.χ. ΑΔΑ), "
    "τα ποσά, τη σειρά του περιεχομένου, τη γλώσσα του κειμένου."
)

_CLEAN_SYSTEM_PROMPT = (
    f"{_ROLE}\n\n"
    "ΔΙΟΡΘΩΣΕΙΣ ΠΟΥ ΚΑΝΕΙΣ (το κείμενο προέρχεται από εξαγωγή PDF/OCR):\n"
    "1. Ορθογραφικά και συντακτικά λάθη.\n"
    "2. Λέξεις και προτάσεις που έσπασαν από την αναδίπλωση γραμμών του PDF.\n"
    "3. Στοίχιση και παραγράφους, ώστε το κείμενο να διαβάζεται καθαρά.\n\n"
    f"{_CONSTRAINTS}\n\n"
    f"{_OUTPUT_CONTRACT}"
)

_MARKDOWN_SYSTEM_PROMPT = (
    f"{_ROLE}\n\n"
    "ΔΙΟΡΘΩΣΕΙΣ ΠΟΥ ΚΑΝΕΙΣ (το κείμενο προέρχεται από εξαγωγή PDF/OCR):\n"
    "1. Ορθογραφικά και συντακτικά λάθη.\n"
    "2. Λέξεις και προτάσεις που έσπασαν από την αναδίπλωση γραμμών του PDF.\n"
    "3. Μορφοποίηση σε καθαρό Markdown: επικεφαλίδες (#, ##), παραγράφους, λίστες (-, 1.) και "
    "πίνακες (| ... |) όπου ταιριάζει στη δομή του εγγράφου. Χωρίς code fences (```).\n\n"
    f"{_CONSTRAINTS}\n\n"
    f"{_OUTPUT_CONTRACT}"
)


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
            temperature=0.0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": chunk},
            ],
        )
    except OpenAIError as exc:
        raise TextCorrectionError(f"OpenAI request failed: {exc}") from exc

    choice = response.choices[0]
    usage = response.usage
    log_event(
        logger,
        "text_correction_chunk_done",
        finish_reason=choice.finish_reason,
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
    )
    return (choice.message.content or "").strip()


def _correct_chunks_in_parallel(chunks: list[str], system_prompt: str, *, log_label: str) -> list[str]:
    total = len(chunks)
    workers = min(total, max(1, settings.document_correction_chunk_parallelism))
    results: list[str | None] = [None] * total

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for index, chunk in enumerate(chunks):
            future = pool.submit(_correct_chunk, chunk, system_prompt)
            futures[future] = index
            log_event(logger, "text_correction_chunk_sending", mode=log_label, chunk=index + 1, total=total)

        for received, future in enumerate(as_completed(futures), start=1):
            index = futures[future]
            results[index] = future.result()
            log_event(
                logger,
                "text_correction_chunk_received",
                mode=log_label,
                chunk=index + 1,
                total=total,
                received=received,
            )

    return results  # type: ignore[return-value]


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

    chunk_budget = max(1, settings.document_chunk_tokens)
    chunks = [text] if token_count <= chunk_budget else _split_into_chunks(text, chunk_budget)

    log_event(
        logger,
        "text_correction_started",
        mode=log_label,
        token_count=token_count,
        chunk_budget=chunk_budget,
        chunk_count=len(chunks),
    )

    try:
        if len(chunks) == 1:
            corrected_chunks = [_correct_chunk(chunks[0], system_prompt)]
        else:
            corrected_chunks = _correct_chunks_in_parallel(chunks, system_prompt, log_label=log_label)
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
