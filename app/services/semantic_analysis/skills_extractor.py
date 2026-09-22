from __future__ import annotations

import json
from threading import Lock

from openai import OpenAI, OpenAIError

from document_processor_service.app.core.config import settings as core_settings
from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.services.semantic_analysis.contracts import SkillMatch
from document_processor_service.app.services.semantic_analysis.exceptions import SkillsDetectionError
from document_processor_service.app.services.semantic_analysis.settings import settings as retrieval_settings
from document_processor_service.app.services.semantic_analysis.skills_index import SkillCandidate, is_ready, search

logger = get_logger("services.document_processor_service.semantic_analysis.skills")

MAX_CHARS = 12000

# Combines the "gate" and "extract" steps from the design into one call: most documents will be
# irrelevant, and a single well-formed prompt determines that just as reliably as a separate
# yes/no call would, at half the latency/cost.
_EXTRACT_PROMPT = (
    "Είσαι αναλυτής διοικητικών αποφάσεων του Διαύγεια. Έλεγξε αν το έγγραφο αφορά δεξιότητες, "
    "ικανότητες ή προσόντα προσωπικού (π.χ. πρόσληψη/προκήρυξη με απαιτούμενα προσόντα, περιγραφή "
    "θέσης, εκπαίδευση, πιστοποίηση, αξιολόγηση προσόντων). Τα περισσότερα έγγραφα ΔΕΝ αφορούν κάτι "
    "τέτοιο.\n\n"
    'Αν δεν αφορά, απάντησε {"relevant": false, "skills": []}.\n\n'
    "Αν αφορά, για κάθε συγκεκριμένη δεξιότητα που εντοπίζεις δώσε:\n"
    "- 'description': σύντομη ΕΛΕΥΘΕΡΗ περιγραφή της δεξιότητας στα ελληνικά (ΟΧΙ κωδικό, π.χ. "
    "'διαχείριση βάσεων δεδομένων', όχι 'ESCO#123').\n"
    "- 'evidence': ΚΥΡΙΟΛΕΚΤΙΚΟ απόσπασμα από το κείμενο (έως 200 χαρακτήρες) που την τεκμηριώνει.\n\n"
    "Απάντησε ΑΠΟΚΛΕΙΣΤΙΚΑ με έγκυρο JSON, χωρίς κείμενο πριν ή μετά:\n"
    '{"relevant": true|false, "skills": [{"description": "...", "evidence": "..."}]}'
)

_client: OpenAI | None = None
_client_lock = Lock()


def _get_client() -> OpenAI:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = OpenAI(api_key=core_settings.openai_api_key)
        return _client


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _chat_json(system_prompt: str, user_content: str, *, max_tokens: int) -> dict:
    try:
        response = _get_client().chat.completions.create(
            model=core_settings.openai_model,
            max_tokens=max_tokens,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
    except OpenAIError as exc:
        raise SkillsDetectionError(f"OpenAI request failed: {exc}") from exc

    raw = (response.choices[0].message.content or "").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillsDetectionError(f"OpenAI returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SkillsDetectionError("OpenAI response was valid JSON but not a JSON object.")
    return payload


def _extract_mentions(excerpt: str) -> list[dict[str, str]]:
    payload = _chat_json(_EXTRACT_PROMPT, excerpt, max_tokens=core_settings.max_tokens)
    if not payload.get("relevant"):
        return []

    items = payload.get("skills")
    if not isinstance(items, list):
        return []

    normalized_excerpt = _normalize(excerpt)
    mentions: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if not description or not evidence:
            continue
        # Same anti-hallucination check as llm_extractor.py: evidence must be real, not plausible.
        if evidence not in excerpt and _normalize(evidence) not in normalized_excerpt:
            continue
        mentions.append({"description": description, "evidence": evidence})

    return mentions


def _select_candidates(description: str, evidence: str, candidates: list[SkillCandidate]) -> list[SkillMatch]:
    candidates_block = "\n".join(f"{i + 1}. uri={c.uri}\n   label: {c.label}" for i, c in enumerate(candidates))
    system_prompt = (
        "Στο έγγραφο εντοπίστηκε η ακόλουθη δεξιότητα:\n"
        f"Περιγραφή: {description}\n"
        f"Απόσπασμα κειμένου: {evidence}\n\n"
        "Παρακάτω είναι υποψήφιες αντιστοιχίσεις από το ESCO taxonomy, ανακτημένες με semantic "
        "search πάνω στην περιγραφή. Επίλεξε ΜΟΝΟ αυτές που ΟΝΤΩΣ αντιστοιχούν στο νόημα — όχι απλώς "
        "λεξιλογική ομοιότητα. Μπορείς να μην επιλέξεις καμία αν καμία δεν ταιριάζει πραγματικά.\n\n"
        f"{candidates_block}\n\n"
        "Απάντησε ΑΠΟΚΛΕΙΣΤΙΚΑ με έγκυρο JSON, επιλέγοντας uri ΜΟΝΟ από την παραπάνω λίστα:\n"
        '{"selections": [{"uri": "...", "confidence": "high"|"medium"|"low"}, ...]}'
    )
    payload = _chat_json(system_prompt, "Επίλεξε τις σωστές αντιστοιχίσεις.", max_tokens=500)

    selections = payload.get("selections")
    if not isinstance(selections, list):
        return []

    by_uri = {c.uri: c for c in candidates}
    matches: list[SkillMatch] = []
    for selection in selections:
        if not isinstance(selection, dict):
            continue
        candidate = by_uri.get(selection.get("uri"))
        if candidate is None:
            continue  # model must pick from the given candidate set; anything else is dropped
        confidence = selection.get("confidence")
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        matches.append(
            SkillMatch(
                taxonomy="ESCO",
                label=candidate.label,
                evidence=evidence,
                confidence=confidence,
                source_uri=candidate.uri,
            )
        )
    return matches


def extract_skills(text: str) -> list[SkillMatch]:
    """Gate+extract (1 LLM call) -> FAISS retrieval against the ESCO index (local, free) ->
    select (1 LLM call per detected skill mention, only over its own retrieved candidates)."""
    if not core_settings.openai_api_key:
        raise SkillsDetectionError("OPENAI_API_KEY is not configured.")

    excerpt = text[:MAX_CHARS]
    if not excerpt.strip():
        return []

    mentions = _extract_mentions(excerpt)
    if not mentions:
        log_event(logger, "skills_detected", count=0)
        return []

    if not is_ready():
        raise SkillsDetectionError(
            f"{len(mentions)} candidate skill mention(s) found, but the ESCO index isn't built yet "
            "— run build_skills_index.py."
        )

    matches: list[SkillMatch] = []
    for mention in mentions:
        candidates = search(mention["description"], top_n=retrieval_settings.semantic_analysis_top_n_skills)
        if not candidates:
            continue
        matches.extend(_select_candidates(mention["description"], mention["evidence"], candidates))

    log_event(logger, "skills_detected", mentions=len(mentions), count=len(matches))
    return matches
