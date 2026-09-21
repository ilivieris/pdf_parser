from __future__ import annotations

from document_processor_service.app.core.config import settings


def backend_summary() -> dict[str, object]:
    return {
        "local_storage": {
            "output_root": settings.document_output_root,
        },
    }
