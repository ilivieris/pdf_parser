from __future__ import annotations

import json
from threading import Lock

from openai import OpenAI, OpenAIError

from document_processor_service.app.core.config import settings
from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.services.semantic_analysis.budget_code_extractor import (
    CODE_FORMAT_PATTERN as _BUDGET_CODE_FORMAT,
)
from document_processor_service.app.services.semantic_analysis.contracts import (
    BudgetCodeMatch,
    CpvMatch,
    DecisionType,
)
from document_processor_service.app.services.semantic_analysis.cpv_extractor import (
    CODE_FORMAT_PATTERN as _CPV_CODE_FORMAT,
)
from document_processor_service.app.services.semantic_analysis.decision_type_reference import load_decision_types
from document_processor_service.app.services.semantic_analysis.exceptions import LlmExtractionError

logger = get_logger("services.document_processor_service.semantic_analysis.llm_extractor")

# Keep the prompt small; the fields we care about (header, operative part) are almost always
# in the first few thousand characters of a Diavgeia decision.
MAX_CHARS = 15000

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


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _evidence_found(evidence: str, text: str, normalized_text: str) -> bool:
    """The core anti-hallucination check: the model's evidence must be a real, verbatim
    (or whitespace-normalized) substring of the document — not just plausible-looking."""
    if not evidence:
        return False
    if evidence in text:
        return True
    return _normalize(evidence) in normalized_text


def _build_system_prompt(types: dict[str, str]) -> str:
    catalogue = "\n".join(f"{uid}\t{label}" for uid, label in sorted(types.items()))
    return (
        "Είσαι αναλυτής διοικητικών αποφάσεων του συστήματος Διαύγεια (diavgeia.gov.gr). "
        "Λαμβάνεις το πλήρες κείμενο ενός δημόσιου εγγράφου (πιθανώς μετά από OCR, άρα με πιθανό "
        "θόρυβο/λάθη αναγνώρισης χαρακτήρων) και εκτελείς τρία ανεξάρτητα έργα εξαγωγής:\n\n"
        "1) ΤΥΠΟΣ ΠΡΑΞΗΣ — ο τύπος πράξης συνήθως ΔΕΝ αναγράφεται ρητά· συμπέρανέ τον από τη δομή, "
        "το θέμα και το περιεχόμενο του εγγράφου. Επίλεξε ΕΝΑ uid ΑΚΡΙΒΩΣ από τον παρακάτω κατάλογο "
        "(uid <TAB> επίσημη ετικέτα). Ποτέ μην επινοήσεις νέο uid ή τροποποιήσεις μια ετικέτα:\n\n"
        f"{catalogue}\n\n"
        "2) CPV — 8ψήφιοι κωδικοί CPV (π.χ. '30192000-1'), ΜΟΝΟ αν αναγράφονται ρητά στο κείμενο.\n\n"
        "3) ΑΛΕ/ΚΑΕ — κωδικοί Αναλυτικού Λογαριασμού Εξόδων (ΑΛΕ, το τρέχον σύστημα από ~2018) ή ο "
        "παλαιότερος Κωδικός Αριθμός Εξόδου (ΚΑΕ), ΜΟΝΟ αν αναγράφονται ρητά.\n\n"
        "ΚΡΙΣΙΜΟΙ ΚΑΝΟΝΕΣ:\n"
        "- Κάθε πεδίο 'evidence' ΠΡΕΠΕΙ να είναι ΚΥΡΙΟΛΕΚΤΙΚΟ, αυτούσιο απόσπασμα από το κείμενο "
        "εισόδου (character-for-character αντιγραφή, όχι παράφραση, διόρθωση ή μετάφραση), έως 200 "
        "χαρακτήρες. Αν δεν μπορείς να βρεις κυριολεκτικό απόσπασμα για μια τιμή, ΜΗΝ τη συμπεριλάβεις.\n"
        "- Μην επινοήσεις ΠΟΤΕ κωδικό (CPV, ΑΛΕ/ΚΑΕ) που δεν εμφανίζεται κυριολεκτικά στο κείμενο.\n"
        "- Αν το έγγραφο δεν περιέχει CPV ή ΑΛΕ/ΚΑΕ, γύρνα κενές λίστες γι' αυτά.\n"
        "- decision_type.uid ΠΡΕΠΕΙ να υπάρχει ΑΚΡΙΒΩΣ στον παραπάνω κατάλογο (ίδια ορθογραφία).\n"
        "- Αν διστάζεις ανάμεσα σε τύπους πράξης, βάλε confidence 'low' και γέμισε το alternative_uids "
        "αντί να μαντέψεις με confidence 'high'.\n\n"
        "Απάντησε ΑΠΟΚΛΕΙΣΤΙΚΑ με έγκυρο JSON, χωρίς κανένα άλλο κείμενο πριν ή μετά, σε αυτό το σχήμα:\n"
        "{\n"
        '  "decision_type": {"uid": "...", "label": "...", "evidence": "...", '
        '"confidence": "high"|"low", "alternative_uids": ["...", ...]},\n'
        '  "cpv": [{"code": "...", "evidence": "..."}],\n'
        '  "ale": [{"kind": "ΑΛΕ"|"ΚΑΕ", "code": "...", "description": "..."|null, "evidence": "..."}]\n'
        "}"
    )


def _verify_decision_type(
    item: object, types: dict[str, str], text: str, normalized_text: str
) -> DecisionType | None:
    if not isinstance(item, dict):
        return None

    uid = item.get("uid")
    label = item.get("label")
    if not uid or not label:
        return None

    evidence = str(item.get("evidence") or "")
    confidence = item.get("confidence") if item.get("confidence") in ("high", "low") else "low"
    alternatives = [alt for alt in (item.get("alternative_uids") or []) if isinstance(alt, str)]

    confirmed = uid in types and types[uid] == label and _evidence_found(evidence, text, normalized_text)

    return DecisionType(
        uid=str(uid),
        label=str(label),
        evidence=evidence,
        confidence=confidence,
        confirmed=confirmed,
        alternative_uids=alternatives,
    )


def _verify_cpv(items: object, text: str, normalized_text: str) -> list[CpvMatch]:
    if not isinstance(items, list):
        return []

    matches: list[CpvMatch] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)

        evidence = str(item.get("evidence") or "")
        confirmed = bool(_CPV_CODE_FORMAT.match(code)) and _evidence_found(evidence, text, normalized_text)
        matches.append(CpvMatch(code=code, evidence=evidence, confirmed=confirmed, source="llm"))

    return matches


def _verify_budget_codes(items: object, text: str, normalized_text: str) -> list[BudgetCodeMatch]:
    if not isinstance(items, list):
        return []

    matches: list[BudgetCodeMatch] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in ("ΚΑΕ", "ΑΛΕ"):
            continue
        code = str(item.get("code") or "").strip()
        if not code or (kind, code) in seen:
            continue
        seen.add((kind, code))

        evidence = str(item.get("evidence") or "")
        description = item.get("description")
        description = description.strip() if isinstance(description, str) and description.strip() else None

        confirmed = bool(_BUDGET_CODE_FORMAT.match(code)) and _evidence_found(evidence, text, normalized_text)
        matches.append(
            BudgetCodeMatch(
                kind=kind,
                code=code,
                description=description,
                evidence=evidence,
                confirmed=confirmed,
                source="llm",
            )
        )

    return matches


def extract_llm_fields(text: str) -> dict[str, object]:
    """One OpenAI call for decision_type + CPV + ΑΛΕ/ΚΑΕ, each result verified deterministically
    against the source text before being trusted (see _evidence_found / format-pattern checks)."""
    if not settings.openai_api_key:
        raise LlmExtractionError("OPENAI_API_KEY is not configured.")

    excerpt = text[:MAX_CHARS]
    if not excerpt.strip():
        return {"decision_type": None, "cpv": [], "budget_codes": []}

    types = load_decision_types()

    try:
        response = _get_client().chat.completions.create(
            model=settings.openai_model,
            max_tokens=settings.max_tokens,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _build_system_prompt(types)},
                {"role": "user", "content": excerpt},
            ],
        )
    except OpenAIError as exc:
        raise LlmExtractionError(f"OpenAI request failed: {exc}") from exc

    raw = (response.choices[0].message.content or "").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmExtractionError(f"OpenAI returned invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise LlmExtractionError("OpenAI response was valid JSON but not a JSON object.")

    normalized_text = _normalize(text)

    decision_type = _verify_decision_type(payload.get("decision_type"), types, text, normalized_text)
    cpv_matches = _verify_cpv(payload.get("cpv"), text, normalized_text)
    budget_matches = _verify_budget_codes(payload.get("ale"), text, normalized_text)

    log_event(
        logger,
        "llm_extraction_finished",
        decision_type_confirmed=bool(decision_type and decision_type.confirmed),
        cpv_count=len(cpv_matches),
        cpv_confirmed=sum(1 for m in cpv_matches if m.confirmed),
        budget_code_count=len(budget_matches),
        budget_code_confirmed=sum(1 for m in budget_matches if m.confirmed),
    )

    return {"decision_type": decision_type, "cpv": cpv_matches, "budget_codes": budget_matches}
