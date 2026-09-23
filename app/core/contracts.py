from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PostProcessing = Literal["none", "clean", "markdown"]

POST_PROCESSING_DESCRIPTION = (
    "'none': return the raw extracted text. "
    "'clean': send the text to OpenAI to fix spelling/grammar and layout, returned as plain text. "
    "'markdown': send the text to OpenAI to fix spelling/grammar and format it as markdown in one pass "
    "(its own prompt — not built on top of 'clean')."
)


class ExtractResponse(BaseModel):
    filename: str = Field(description="Original name of the uploaded file.")
    download_url: str = Field(
        description="Presigned GET URL for the extracted text; expires after MINIO_URL_EXPIRY_SECONDS."
    )
    note: str | None = Field(
        default=None,
        description=(
            "Set when something noteworthy happened: correction was skipped because the document "
            "was too large, or correction failed (e.g. an OpenAI error, or the model returned a "
            "truncated/summarized result) and the output holds the original, uncorrected text instead."
        ),
    )


HealthStatus = Literal["healthy", "degraded"]
CheckStatus = Literal["pass", "fail"]


class HealthCheck(BaseModel):
    name: str = Field(description="Dependency that was probed.")
    status: CheckStatus
    latency_ms: float = Field(description="Wall-clock duration of the probe.")
    detail: str | None = Field(
        default=None, description="Why the probe failed. Absent when it passed."
    )


class HealthResponse(BaseModel):
    status: HealthStatus = Field(
        description="'healthy' when every check passed, 'degraded' when at least one failed."
    )
    service: str
    version: str
    uptime_seconds: float
    checks: list[HealthCheck] = Field(default_factory=list)


class LivenessResponse(BaseModel):
    """Answers 'is this process running', with no dependency probes. Always 200."""

    status: Literal["alive"]
    service: str
    version: str
    uptime_seconds: float
