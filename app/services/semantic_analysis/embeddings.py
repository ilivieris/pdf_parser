from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request

import numpy as np

from document_processor_service.app.services.semantic_analysis.exceptions import SemanticAnalysisError
from document_processor_service.app.services.semantic_analysis.settings import settings

REQUEST_TIMEOUT = 60
# Transient failures (dropped keep-alive connections, truncated reads) happen in practice under
# sustained request volume — e.g. an IncompleteRead partway through a ~200-batch index build.
# HTTPError (a clean 4xx/5xx response) is NOT retried: that's deterministic, retrying won't help.
RETRY_DELAYS = (1, 3, 6)


class EmbeddingServiceError(SemanticAnalysisError):
    """Raised when the remote embeddings API is unreachable or returns something unexpected."""


def _post(texts: list[str], input_type: str) -> list[list[float]]:
    body = json.dumps(
        {"input": texts, "input_type": input_type, "encoding_format": "float"}
    ).encode("utf-8")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.semantic_analysis_embedding_api_key:
        headers["Authorization"] = f"Bearer {settings.semantic_analysis_embedding_api_key}"

    request = urllib.request.Request(
        settings.semantic_analysis_embedding_api_url, data=body, headers=headers, method="POST"
    )

    last_error: Exception | None = None
    for delay in (0, *RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            raise EmbeddingServiceError(f"Embeddings API request failed: {exc}") from exc
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError) as exc:
            last_error = exc
    else:
        raise EmbeddingServiceError(
            f"Embeddings API request failed after {len(RETRY_DELAYS) + 1} attempts: {last_error}"
        ) from last_error

    try:
        items = sorted(payload["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in items]
    except (KeyError, TypeError) as exc:
        raise EmbeddingServiceError(f"Embeddings API response had an unexpected shape: {exc}") from exc


def _encode(texts: list[str], input_type: str) -> np.ndarray:
    batch_size = settings.semantic_analysis_embedding_batch_size
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        vectors.extend(_post(texts[start : start + batch_size], input_type))

    matrix = np.asarray(vectors, dtype="float32")
    # The API doesn't guarantee normalized output; FAISS IndexFlatIP == cosine similarity only
    # over unit vectors, and our similarity threshold assumes that too.
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def embed_passages(texts: list[str]) -> np.ndarray:
    """Embed reference texts (e.g. ESCO skill labels) for indexing."""
    return _encode(texts, settings.semantic_analysis_passage_input_type)


def embed_queries(texts: list[str]) -> np.ndarray:
    """Embed a search query (e.g. an LLM-extracted skill description) for retrieval."""
    return _encode(texts, settings.semantic_analysis_query_input_type)
