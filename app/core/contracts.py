from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PostProcessing = Literal["none", "clean", "markdown"]


class ExtractRequest(BaseModel):
    path: str = Field(description="Path to the source file, resolved on the server's local disk.")
    filename: str | None = Field(
        default=None,
        description=(
            "Optional filename hint used to pick the parser and name the output file. "
            "Only needed when `path` itself has no file extension; if provided together with "
            "an extension in `path`, the extensions must match."
        ),
    )
    post_processing: PostProcessing = Field(
        default="none",
        description=(
            "'none': return the raw extracted text. "
            "'clean': send the text to OpenAI to fix spelling/grammar and layout, returned as plain text. "
            "'markdown': send the text to OpenAI to fix spelling/grammar and format it as markdown in one pass "
            "(its own prompt — not built on top of 'clean')."
        ),
    )


class ExtractResponse(BaseModel):
    filename: str
    source_path: str
    artifact_id: str
    text_path: str
    note: str | None = Field(
        default=None,
        description=(
            "Set when something noteworthy happened: correction was skipped because the document "
            "was too large, or correction failed (e.g. an OpenAI error, or the model returned a "
            "truncated/summarized result) and `text_path` holds the original, uncorrected text instead."
        ),
    )
