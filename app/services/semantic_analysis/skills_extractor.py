from __future__ import annotations

import json
from threading import Lock

from openai import OpenAI, OpenAIError

from document_processor_service.app.core.config import settings
from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.services.semantic_analysis.contracts import SkillMatch
from document_processor_service.app.services.semantic_analysis.exceptions import SkillsDetectionError

logger = get_logger("services.document_processor_service.semantic_analysis.skills")

# There is no bundled ESCO/DigComp reference dataset here (ESCO alone has ~13,000 skill entries),
# so this asks the model to propose candidates instead of validating against the real registries.
# Treat every SkillMatch as a lead to check against the official ESCO API / DigComp framework,
# not as ground truth.
MAX_CHARS = 12000

_SYSTEM_PROMPT = (
    "Είσαι αναλυτής διοικητικών αποφάσεων του Διαύγεια. Ελέγχεις αν μια απόφαση αναφέρεται σε "
    "δεξιότητες, ικανότητες ή επάρκειες προσωπικού (π.χ. εκπαίδευση, πιστοποίηση, περιγραφή θέσης, "
    "αξιολόγηση προσόντων, πρόσληψη/προκήρυξη με απαιτούμενα προσόντα) και, αν ναι, εντοπίζεις ποιες "
    "συγκεκριμένες δεξιότητες αναφέρονται.\n\n"
    "Χρησιμοποίησε δύο ταξινομίες ως σημείο αναφοράς:\n"
    "- ESCO (European Skills, Competences, Qualifications and Occupations): δεξιότητες/επαγγέλματα.\n"
    "- DigComp: οι 5 βασικοί τομείς ψηφιακής επάρκειας — (1) Πληροφοριακός και δεδομενικός γραμματισμός, "
    "(2) Επικοινωνία και συνεργασία, (3) Δημιουργία ψηφιακού περιεχομένου, (4) Ασφάλεια, "
    "(5) Επίλυση προβλημάτων.\n\n"
    "Επίστρεψε ΑΠΟΚΛΕΙΣΤΙΚΑ έγκυρο JSON, χωρίς κείμενο πριν ή μετά, της μορφής:\n"
    '{"skills": [{"taxonomy": "ESCO"|"DigComp", "label": "...", '
    '"evidence": "<αυτολεξεί απόσπασμα από το κείμενο>", "confidence": "high"|"medium"|"low"}]}\n\n'
    'Αν το έγγραφο δεν αναφέρεται καθόλου σε δεξιότητες, επίστρεψε {"skills": []}. '
    "Μην επινοήσεις απόσπασμα που δεν υπάρχει αυτολεξεί στο κείμενο."
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


def extract_skills(text: str) -> list[SkillMatch]:
    if not settings.openai_api_key:
        raise SkillsDetectionError("OPENAI_API_KEY is not configured.")

    excerpt = text[:MAX_CHARS]
    if not excerpt.strip():
        return []

    try:
        response = _get_client().chat.completions.create(
            model=settings.openai_model,
            max_tokens=settings.max_tokens,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": excerpt},
            ],
        )
    except OpenAIError as exc:
        raise SkillsDetectionError(f"OpenAI request failed: {exc}") from exc

    raw = (response.choices[0].message.content or "").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillsDetectionError(f"OpenAI returned invalid JSON: {exc}") from exc

    items = payload.get("skills", [])
    if not isinstance(items, list):
        raise SkillsDetectionError("OpenAI response's 'skills' field was not a list.")

    matches: list[SkillMatch] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            matches.append(
                SkillMatch(
                    taxonomy=item["taxonomy"],
                    label=str(item["label"]),
                    evidence=str(item["evidence"]),
                    confidence=item.get("confidence", "low"),
                )
            )
        except (KeyError, ValueError):
            continue

    log_event(logger, "skills_detected", count=len(matches))
    return matches
