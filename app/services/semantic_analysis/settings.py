from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class SkillsRetrievalSettings(BaseSettings):
    """Config for the ESCO skills-retrieval feature only. Kept local to this folder (rather than
    in core/config.py) so the whole feature — code, data and config — is one directory to delete."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Remote embeddings service (deployed separately, GPU-backed) — no local model/torch needed.
    # POST {input: [...], input_type: "query"|"passage"|"none", encoding_format: "float"}
    # -> OpenAI-style {"data": [{"embedding": [...]}], "model": "...", ...}
    semantic_analysis_embedding_api_url: str = "https://llm-serving.eu-dev.novelcore.org/v1/embeddings/text"
    semantic_analysis_embedding_api_key: str = ""  # sent as `Authorization: Bearer <key>` if set

    # The API takes care of any model-specific query/passage handling itself via `input_type` —
    # these just control which value is sent for each of our two use cases. Configurable in case
    # the deployed model's accepted values ever change (e.g. to "none" to disable it).
    semantic_analysis_query_input_type: str = "query"
    semantic_analysis_passage_input_type: str = "passage"

    # The deployed embeddings API rejects more than 64 inputs per request (confirmed 2026-09-22).
    semantic_analysis_embedding_batch_size: int = 64
    semantic_analysis_top_n_skills: int = 10

    # Cosine similarity floor below which a FAISS hit is discarded as "no match" rather than
    # returned as the closest-but-irrelevant candidate.
    semantic_analysis_match_threshold: float = 0.75


settings = SkillsRetrievalSettings()
