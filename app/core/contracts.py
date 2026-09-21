from __future__ import annotations

from pydantic import BaseModel


class ExtractRequest(BaseModel):
    path: str
    filename: str | None = None
    export_markdown: bool = False
    correct_text: bool = False


class ExtractResponse(BaseModel):
    filename: str
    source_path: str
    artifact_id: str
    text_path: str
    char_count: int
    text_preview: str
    markdown_path: str | None = None
    markdown_char_count: int | None = None
    corrected_path: str | None = None
    corrected_char_count: int | None = None
    corrected_text_preview: str | None = None
