"""(Re-)build of the ESCO skills FAISS index.

Walks the ESCO concept hierarchy via the public REST API (~13k skills in Greek — see
esco_client.py for why it's a hierarchy walk rather than a paginated search), embeds each skill's
label via the remote embeddings API (see settings.py), and writes a FAISS index + parallel
metadata file under data/.

Runs automatically on app startup when the index isn't already on disk (see startup.py). To force
a rebuild (e.g. after the deployed embedding model changes), delete data/esco_skills.faiss and
data/esco_skills_meta.jsonl and restart, or run this module directly:
    python -m document_processor_service.app.services.semantic_analysis.build_skills_index
"""

from __future__ import annotations

import json

from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.services.semantic_analysis.embeddings import embed_passages
from document_processor_service.app.services.semantic_analysis.esco_client import iter_esco_skills
from document_processor_service.app.services.semantic_analysis.settings import settings
from document_processor_service.app.services.semantic_analysis.skills_index import (
    DATA_DIR,
    INDEX_PATH,
    META_PATH,
)

logger = get_logger("services.document_processor_service.semantic_analysis.build_index")

# How often to emit a progress log line, in items.
_LOG_EVERY = 1000
_LOG_EVERY_BATCHES = 20


def build(*, language: str = "el") -> int:
    """Returns the number of skills indexed."""
    import faiss
    import numpy as np

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log_event(logger, "esco_crawl_started", language=language)
    records: list[dict] = []
    texts: list[str] = []
    for skill in iter_esco_skills(language=language):
        if not skill.label:
            continue
        records.append({"uri": skill.uri, "label": skill.label, "skill_type": skill.skill_type})
        texts.append(skill.label)
        if len(records) % _LOG_EVERY == 0:
            log_event(logger, "esco_crawl_progress", found=len(records))
    log_event(logger, "esco_crawl_finished", found=len(records))

    batch_size = settings.semantic_analysis_embedding_batch_size
    batch_count = (len(texts) + batch_size - 1) // batch_size
    log_event(logger, "esco_embedding_started", total=len(texts), batch_size=batch_size, batches=batch_count)

    vectors = []
    for batch_index, start in enumerate(range(0, len(texts), batch_size), start=1):
        batch = texts[start : start + batch_size]
        vectors.append(embed_passages(batch))
        if batch_index % _LOG_EVERY_BATCHES == 0 or batch_index == batch_count:
            log_event(
                logger,
                "esco_embedding_progress",
                embedded=min(start + batch_size, len(texts)),
                total=len(texts),
                batch=batch_index,
                of_batches=batch_count,
            )
    matrix = np.vstack(vectors).astype("float32")

    index = faiss.IndexFlatIP(matrix.shape[1])
    index.add(matrix)
    faiss.write_index(index, str(INDEX_PATH))

    with META_PATH.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    log_event(
        logger,
        "esco_index_generated",
        vectors=index.ntotal,
        dim=matrix.shape[1],
        index_path=str(INDEX_PATH),
        meta_path=str(META_PATH),
    )
    return index.ntotal


if __name__ == "__main__":
    build()
