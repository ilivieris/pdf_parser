from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    path: str = Field(
        description=(
            "Path to the source file, read directly from disk — absolute, or relative to the "
            "server's working directory. Not confined to DOCUMENT_OUTPUT_ROOT. The file's own "
            "extension picks the parser."
        )
    )


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
    label: str = Field(description="Candidate skill/competence label proposed by the model.")
    evidence: str = Field(description="Verbatim quote from the document that the model based this match on.")
    confidence: Literal["high", "medium", "low"]


class AnalyzeResponse(BaseModel):
    filename: str
    source_path: str
    artifact_id: str
    decision_type: DecisionType | None = Field(
        default=None, description="Best-guess decision type. None when classification was skipped or unparsable."
    )
    cpv: list[CpvMatch]
    budget_codes: list[BudgetCodeMatch]
    skills: list[SkillMatch] = Field(
        description=(
            "Candidate ESCO/DigComp matches proposed by an LLM. These are NOT validated against the "
            "official ESCO/DigComp registries — treat them as suggestions to review, not ground truth."
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
