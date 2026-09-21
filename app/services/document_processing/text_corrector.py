from __future__ import annotations

from threading import Lock

from openai import OpenAI, OpenAIError

from document_processor_service.app.core.config import settings
from document_processor_service.app.services.document_processing.exceptions import TextCorrectionError

_SYSTEM_PROMPT = (
    "Είσαι διορθωτής κειμένων που έχουν εξαχθεί αυτόματα από PDF/OCR. "
    "Λαμβάνεις ένα κείμενο που μπορεί να έχει ορθογραφικά και συντακτικά λάθη, "
    "σπασμένες προτάσεις λόγω αναδίπλωσης γραμμών, και κακή στοίχιση/μορφοποίηση.\n\n"
    "Κάνε τα εξής:\n"
    "1. Διόρθωσε ορθογραφικά και συντακτικά λάθη.\n"
    "2. Βελτίωσε τη στοίχιση και τη μορφοποίηση (παραγράφους, κενά, δομή) ώστε το κείμενο "
    "να διαβάζεται καθαρά.\n\n"
    "Μην αλλάξεις το νόημα, μην προσθέσεις ή αφαιρέσεις πληροφορίες, αριθμούς, ημερομηνίες, "
    "ονόματα, κωδικούς (π.χ. ΑΔΑ) ή ποσά. Διατήρησε την αρχική γλώσσα του κειμένου. "
    "Επίστρεψε μόνο το διορθωμένο κείμενο, χωρίς πρόσθετα σχόλια."
)

_client: OpenAI | None = None
_client_lock = Lock()


def _get_client() -> OpenAI:
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is None:
            _client = OpenAI(api_key=settings.openai_api_key)
        return _client


def correct_text(text: str) -> str:
    if not text.strip():
        return text
    if not settings.openai_api_key:
        raise TextCorrectionError("OPENAI_API_KEY is not configured.")

    try:
        response = _get_client().chat.completions.create(
            model=settings.openai_model,
            max_tokens=settings.max_tokens,
            temperature=0.2,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
    except OpenAIError as exc:
        raise TextCorrectionError(f"OpenAI request failed: {exc}") from exc

    corrected = (response.choices[0].message.content or "").strip()
    if not corrected:
        raise TextCorrectionError("OpenAI returned an empty correction.")
    return corrected
