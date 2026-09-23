from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CpvMatch(BaseModel):
    code: str = Field(description="8-digit CPV code, with the check digit (e.g. '30192000-1') when present.")
    evidence: str = Field(description="Text snippet containing the code, for manual verification.")
    confirmed: bool = Field(
        description=(
            "True when the code+evidence passed deterministic verification: for source='regex', "
            "always true (it's a direct pattern match on the real text). For source='llm', true only "
            "when the evidence is a verbatim substring of the document AND the code matches the CPV "
            "format — false means treat it as an unverified LLM suggestion."
        )
    )
    source: Literal["regex", "llm"]


class BudgetCodeMatch(BaseModel):
    kind: Literal["ΚΑΕ", "ΑΛΕ"]
    code: str
    description: str | None = Field(default=None, description="Row/line description, when one was found next to the code.")
    evidence: str = Field(description="Text snippet (or table row) containing the code, for manual verification.")
    confirmed: bool = Field(
        description="Same semantics as CpvMatch.confirmed: always true for source='regex', verified for source='llm'."
    )
    source: Literal["regex", "llm"]


class DecisionType(BaseModel):
    uid: str = Field(description="decisionTypeUid, as defined by the official Diavgeia catalogue.")
    label: str
    evidence: str = Field(description="Text snippet the model based this classification on.")
    confidence: Literal["high", "low"] = Field(description="The model's own self-reported confidence.")
    confirmed: bool = Field(
        description=(
            "True only when: uid exists in the bundled decision-type catalogue, its label matches "
            "exactly, AND evidence is a verbatim substring of the document. False means the model's "
            "answer failed verification — do not trust it without review."
        )
    )
    alternative_uids: list[str] = Field(default_factory=list, description="Other uids the model considered.")


class SkillMatch(BaseModel):
    taxonomy: Literal["ESCO", "DigComp"]
    label: str = Field(description="The matched taxonomy entry's own official label (not LLM free text).")
    evidence: str = Field(description="Verbatim quote from the document that the model based this match on.")
    confidence: Literal["high", "medium", "low"]
    source_uri: str | None = Field(
        default=None,
        description=(
            "URI of the matched entry in the source taxonomy (e.g. the ESCO skill concept URI). "
            "None only if the taxonomy has no bundled index yet (currently: DigComp)."
        ),
    )


class AnalyzeResponse(BaseModel):
    filename: str = Field(description="Original name of the uploaded file.")
    artifact_id: str = Field(description="Content hash of the upload; stable across identical files.")
    decision_type: DecisionType | None = Field(
        default=None, description="Best-guess decision type. None when classification was skipped or unparsable."
    )
    cpv: list[CpvMatch]
    budget_codes: list[BudgetCodeMatch]
    skills: list[SkillMatch] = Field(
        description=(
            "ESCO skill matches: an LLM extracts a free-text skill description from the document, "
            "semantic search retrieves candidate ESCO entries from a local FAISS index built from the "
            "real ESCO taxonomy, and the LLM selects which retrieved candidates actually apply — so "
            "every label/source_uri here is a real ESCO entry, not free-form LLM text. DigComp is not "
            "covered yet (see skills_note): the DigComp-to-ESCO mapping is only published in ESCO's "
            "gated bulk-download bundle, which needs a human to complete an email-verification step."
        )
    )
    extraction_note: str | None = Field(
        default=None,
        description="Set when the LLM-based decision_type/CPV/ΑΛΕ pass was skipped or failed (e.g. OPENAI_API_KEY missing).",
    )
    skills_note: str | None = Field(
        default=None,
        description="Set when skills detection was skipped (e.g. OPENAI_API_KEY missing) or ran on a truncated excerpt.",
    )
