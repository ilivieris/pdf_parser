from __future__ import annotations

import time

from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.services.semantic_analysis import build_skills_index
from document_processor_service.app.services.semantic_analysis.skills_index import is_ready

logger = get_logger("services.document_processor_service.semantic_analysis.startup")


def ensure_skills_index_ready() -> None:
    """Called once on app startup (see main.py). Blocks until the ESCO skills index is on disk
    and ready to serve — building it from scratch the first time takes several minutes (crawling
    ESCO + calling the embeddings API), and is skipped entirely on later restarts once built."""
    if is_ready():
        log_event(logger, "esco_index_check", status="already_built")
        return

    log_event(logger, "esco_index_check", status="building")
    started = time.monotonic()
    try:
        vector_count = build_skills_index.build()
    except Exception:
        logger.exception("ESCO skills index build failed — /analyze will run with skills detection degraded.")
        return

    log_event(
        logger,
        "esco_index_check",
        status="build_complete",
        vectors=vector_count,
        duration_seconds=round(time.monotonic() - started, 1),
    )
