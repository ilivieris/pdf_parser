from __future__ import annotations

import anyio
from fastapi import FastAPI, HTTPException

from document_processor_service.app.core.config import settings
from document_processor_service.app.core.contracts import ExtractRequest, ExtractResponse
from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.core.object_storage import ObjectStorageError
from document_processor_service.app.core.service_clients import backend_summary
from document_processor_service.app.services.extraction_pipeline import extract_document_from_local
from document_processor_service.app.services.document_processing.exceptions import (
    ExtractionError,
    TextCorrectionError,
    UnsupportedFormatError,
)

app = FastAPI(title="Document Processor Service", version="0.2.0")
logger = get_logger("services.document_processor_service")
_extraction_limiter = anyio.CapacityLimiter(max(1, settings.document_extraction_workers))


@app.get("/health")
def health() -> dict:
    return {"status": "healthy", "service": "document_processor_service", "backends": backend_summary()}


@app.post("/extract", response_model=ExtractResponse)
async def extract_document(payload: ExtractRequest) -> ExtractResponse:
    log_event(
        logger,
        "document_extract_received",
        path=payload.path,
        export_markdown=payload.export_markdown,
        correct_text=payload.correct_text,
    )

    try:
        result = await anyio.to_thread.run_sync(
            lambda: extract_document_from_local(
                path=payload.path,
                export_markdown=payload.export_markdown,
                correct_text=payload.correct_text,
                filename=payload.filename,
            ),
            limiter=_extraction_limiter,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ExtractionError as exc:
        logger.exception("Extraction failed while parsing the document.")
        raise HTTPException(status_code=422, detail="Document text extraction failed.") from exc
    except TextCorrectionError as exc:
        logger.exception("Text correction failed.")
        raise HTTPException(status_code=502, detail="Text correction failed.") from exc
    except ObjectStorageError as exc:
        logger.exception("Extraction failed because local storage returned an error.")
        raise HTTPException(status_code=502, detail="Extraction failed because local storage is unavailable.") from exc
    except Exception as exc:
        logger.exception("Unexpected extraction failure.")
        raise HTTPException(status_code=500, detail="Extraction failed.") from exc

    log_event(
        logger,
        "document_extract_finished",
        path=result["source_path"],
        text_path=result["text_path"],
        export_markdown=payload.export_markdown,
    )
    return ExtractResponse.model_validate(result)
