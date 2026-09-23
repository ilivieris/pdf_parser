from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


SERVICE_NAME = "document_processor_service"
SERVICE_VERSION = "0.3.0"


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

    # ---- MinIO object storage -------------------------------------------------
    # Host:port (or a full http(s):// URL) of the MinIO server holding both the uploaded
    # sources and the extraction output.
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "documents"
    minio_secure: bool = False
    # Pinned rather than looked up: the region lookup is a network call to the endpoint, which
    # the presigning client (bound to minio_public_endpoint) may not be able to reach from here.
    # "us-east-1" is what MinIO reports unless its own MINIO_REGION says otherwise.
    minio_region: str = "us-east-1"
    # Host the caller reaches MinIO under, when it differs from the one the service dials
    # (e.g. the service reaches MinIO at `minio:9000` inside Compose, while the caller needs
    # `localhost:9000`). The download link is signed for this host. Empty means "use
    # minio_endpoint".
    minio_public_endpoint: str = ""
    # Lifetime of the presigned download URL. 7 days is the S3 presign maximum.
    minio_url_expiry_seconds: int = 604800

    minio_source_prefix: str = "sources"
    minio_output_prefix: str = "extracted"

    # ---- Cleanup --------------------------------------------------------------
    # Delete the uploaded source as soon as its text has been stored. The extracted output
    # and its download link are unaffected. Off by default: keeping the source is what makes
    # a re-run possible without the caller re-uploading.
    minio_delete_source_after_extract: bool = False
    # Expire objects under each prefix after this many days; 0 keeps them forever. Enforced by
    # MinIO's own lifecycle scanner, so it keeps working while this service is down and never
    # breaks a download link that is still inside its expiry window.
    minio_source_retention_days: int = 0
    minio_output_retention_days: int = 0

    # Rejected before anything is uploaded.
    max_upload_size_bytes: int = 26214400

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"
    max_tokens: int = 32768
    document_correction_chunk_parallelism: int = 10
    # Per-chunk input budget in tokens, far below the model's output cap: faithful reproduction
    # degrades into silently dropping content long before that cap is reached. Measured on real
    # Diavgeia/FEK text with gpt-4.1: sizes up to 12000 reproduced every sampled region in full,
    # while 14000+ intermittently stopped a third of the way through (finish_reason still "stop",
    # no placeholder -- the drop is silent). 8000 keeps a margin under that threshold.
    document_chunk_tokens: int = 8000


settings = Settings()
