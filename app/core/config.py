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
    document_correction_chunk_parallelism: int = 4


settings = Settings()
