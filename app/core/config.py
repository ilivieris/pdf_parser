from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    document_extraction_workers: int = 2
    document_ocr_dpi: int = 200
    document_ocr_max_pages: int = 50
    document_ocr_timeout_seconds: int = 120
    document_supported_extensions: list[str] = [
        ".pdf",
        ".docx",
        ".doc",
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".json",
        ".log",
    ]

    document_output_root: str = "./extracted"

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"
    max_tokens: int = 4096
    document_correction_chunk_parallelism: int = 10
    # Per-chunk input budget in tokens, far below the model's output cap: faithful reproduction
    # degrades into silently dropping content long before that cap is reached. Measured on real
    # Diavgeia/FEK text with gpt-4.1: sizes up to 12000 reproduced every sampled region in full,
    # while 14000+ intermittently stopped a third of the way through (finish_reason still "stop",
    # no placeholder -- the drop is silent). 8000 keeps a margin under that threshold.
    document_chunk_tokens: int = 8000


settings = Settings()
