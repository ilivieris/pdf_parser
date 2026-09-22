from __future__ import annotations

import anyio
from fastapi import APIRouter, HTTPException

from document_processor_service.app.core.config import settings
from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.services.document_processing.exceptions import (
    ExtractionError,
    UnsupportedFormatError,
)
from document_processor_service.app.services.semantic_analysis.contracts import AnalyzeRequest, AnalyzeResponse
from document_processor_service.app.services.semantic_analysis.exceptions import SemanticAnalysisError
from document_processor_service.app.services.semantic_analysis.pipeline import analyze_document_from_local

logger = get_logger("services.document_processor_service.semantic_analysis")
router = APIRouter()
_analysis_limiter = anyio.CapacityLimiter(max(1, settings.document_extraction_workers))


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze_document(payload: AnalyzeRequest) -> AnalyzeResponse:
    log_event(logger, "document_analyze_received", path=payload.path)

    try:
        result = await anyio.to_thread.run_sync(
            lambda: analyze_document_from_local(path=payload.path),
            limiter=_analysis_limiter,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ExtractionError as exc:
        logger.exception("Extraction failed while parsing the document for analysis.")
        raise HTTPException(status_code=422, detail="Document text extraction failed.") from exc
    except SemanticAnalysisError as exc:
        logger.exception("Analysis failed while reading the file.")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected analysis failure.")
        raise HTTPException(status_code=500, detail="Analysis failed.") from exc

    return AnalyzeResponse.model_validate(result)
