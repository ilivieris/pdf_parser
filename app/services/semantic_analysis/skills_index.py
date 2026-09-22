from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from document_processor_service.app.services.semantic_analysis.embeddings import embed_queries
from document_processor_service.app.services.semantic_analysis.settings import settings

DATA_DIR = Path(__file__).with_name("data")
INDEX_PATH = DATA_DIR / "esco_skills.faiss"
META_PATH = DATA_DIR / "esco_skills_meta.jsonl"


@dataclass(frozen=True)
class SkillCandidate:
    uri: str
    label: str
    score: float


class SkillsIndexNotBuiltError(RuntimeError):
    """Raised when a search is attempted before build_skills_index.py has been run."""


_index = None
_meta: list[dict] | None = None
_lock = Lock()


def _load() -> None:
    global _index, _meta
    if _index is not None:
        return
    with _lock:
        if _index is not None:
            return
        if not INDEX_PATH.exists() or not META_PATH.exists():
            raise SkillsIndexNotBuiltError(
                f"ESCO skills index not found at {INDEX_PATH}. Build it first with:\n"
                "  python -m document_processor_service.app.services.semantic_analysis.build_skills_index"
            )

        import faiss  # lazy import: heavy dependency, only needed once a search actually runs

        index = faiss.read_index(str(INDEX_PATH))
        meta = [json.loads(line) for line in META_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        if index.ntotal != len(meta):
            raise SkillsIndexNotBuiltError(
                f"Index/metadata mismatch: {index.ntotal} vectors vs {len(meta)} metadata rows "
                f"— rebuild with build_skills_index.py."
            )
        _index, _meta = index, meta


def search(query_text: str, top_n: int | None = None) -> list[SkillCandidate]:
    """Embed `query_text` and return the top-n ESCO skills above the configured similarity floor."""
    _load()
    assert _index is not None and _meta is not None  # for type-checkers; _load() guarantees this

    n = top_n or settings.semantic_analysis_top_n_skills
    query_vector = embed_queries([query_text])
    scores, indices = _index.search(query_vector, n)

    candidates: list[SkillCandidate] = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or score < settings.semantic_analysis_match_threshold:
            continue
        record = _meta[idx]
        candidates.append(SkillCandidate(uri=record["uri"], label=record["label"], score=float(score)))
    return candidates


def is_ready() -> bool:
    return INDEX_PATH.exists() and META_PATH.exists()
