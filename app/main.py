from __future__ import annotations

import time

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile, status

from document_processor_service.app.core.config import SERVICE_NAME, SERVICE_VERSION, settings
from document_processor_service.app.core.contracts import (
    POST_PROCESSING_DESCRIPTION,
    ExtractResponse,
    HealthResponse,
    LivenessResponse,
    PostProcessing,
)
from document_processor_service.app.core.logging import get_logger, log_event
from document_processor_service.app.core.object_storage import ObjectStorageError, get_object_store
from document_processor_service.app.core.service_clients import retention_rules, run_health_checks
from document_processor_service.app.services.extraction_pipeline import extract_document_from_upload
from document_processor_service.app.services.document_processing.exceptions import (
    ExtractionError,
    TextCorrectionError,
    UnsupportedFormatError,
)
# # Self-contained under app/services/semantic_analysis/ — remove this import, the
# # app.include_router(...) call and the startup handler below to drop /analyze entirely.
# from document_processor_service.app.services.semantic_analysis.router import router as semantic_analysis_router
# from document_processor_service.app.services.semantic_analysis.startup import ensure_skills_index_ready

app = FastAPI(title="Document Processor Service", version=SERVICE_VERSION)
logger = get_logger("services.document_processor_service")
_extraction_limiter = anyio.CapacityLimiter(max(1, settings.document_extraction_workers))
_started_at = time.monotonic()
# app.include_router(semantic_analysis_router)


def _uptime_seconds() -> float:
    return round(time.monotonic() - _started_at, 3)


@app.on_event("startup")
def _apply_retention_policy() -> None:
    """Push the configured expiry windows onto the bucket, once, at startup.

    A failure here is logged and swallowed: retention is a housekeeping preference, and a
    bucket whose lifecycle config this account may not write is not a reason to refuse traffic.
    """
    rules = retention_rules()
    if not rules:
        return

    try:
        get_object_store().apply_retention_policy(rules)
        log_event(logger, "retention_policy_applied", rules=rules)
    except ObjectStorageError:
        logger.exception("Could not apply the MinIO retention policy; objects will not expire.")


# @app.on_event("startup")
# async def _build_skills_index_on_startup() -> None:
#     # Blocks the app from accepting requests until the ESCO skills index is ready (building it
#     # from scratch takes several minutes the first time; instant on later restarts).
#     await anyio.to_thread.run_sync(ensure_skills_index_ready)


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Readiness: probes every dependency.",
    responses={503: {"model": HealthResponse, "description": "At least one dependency is down."}},
)
async def health(response: Response) -> HealthResponse:
    checks = await anyio.to_thread.run_sync(run_health_checks)
    healthy = all(check.status == "pass" for check in checks)

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        log_event(
            logger,
            "health_check_degraded",
            failed=[check.name for check in checks if check.status == "fail"],
        )

    return HealthResponse(
        status="healthy" if healthy else "degraded",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        uptime_seconds=_uptime_seconds(),
        checks=checks,
    )


@app.get(
    "/health/live",
    response_model=LivenessResponse,
    summary="Liveness: is the process up. Probes nothing.",
)
def liveness() -> LivenessResponse:
    # Deliberately dependency-free. A liveness probe that fails during a MinIO outage would
    # restart a perfectly healthy container and turn an outage into a crash loop.
    return LivenessResponse(
        status="alive",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        uptime_seconds=_uptime_seconds(),
    )


@app.post("/extract", response_model=ExtractResponse)
async def extract_document(
    file: UploadFile = File(description="The document to upload, extract and store."),
    post_processing: PostProcessing = Form(default="none", description=POST_PROCESSING_DESCRIPTION),
) -> ExtractResponse:
    log_event(
        logger,
        "document_extract_received",
        filename=file.filename,
        content_type=file.content_type,
        post_processing=post_processing,
    )

    data = await file.read()
    await file.close()

    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File is larger than the {settings.max_upload_size_bytes} byte upload limit.",
        )

    try:
        result = await anyio.to_thread.run_sync(
            lambda: extract_document_from_upload(
                filename=file.filename or "",
                data=data,
                post_processing=post_processing,
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
        logger.exception("Extraction failed because object storage returned an error.")
        raise HTTPException(status_code=502, detail="Extraction failed because MinIO is unavailable.") from exc
    except Exception as exc:
        logger.exception("Unexpected extraction failure.")
        raise HTTPException(status_code=500, detail="Extraction failed.") from exc

    return ExtractResponse.model_validate(result)
