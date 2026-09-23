from __future__ import annotations

import time

from document_processor_service.app.core.config import settings
from document_processor_service.app.core.contracts import HealthCheck
from document_processor_service.app.core.object_storage import ObjectStorageError, get_object_store


def check_object_storage() -> HealthCheck:
    """Probe MinIO with a real round-trip, and report how long it took.

    Never raises: a health check that throws tells the caller nothing about *which* dependency
    is down.
    """
    started = time.perf_counter()
    detail: str | None = None
    try:
        get_object_store().ping()
        status = "pass"
    except ObjectStorageError as exc:
        status = "fail"
        detail = str(exc)
    except Exception as exc:  # defensive: an unmapped SDK/network error must not 500 /health
        status = "fail"
        detail = f"{exc.__class__.__name__}: {exc}"

    return HealthCheck(
        name=f"minio:{settings.minio_endpoint}/{settings.minio_bucket}",
        status=status,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        detail=detail,
    )


def run_health_checks() -> list[HealthCheck]:
    return [check_object_storage()]


def retention_rules() -> dict[str, int]:
    """Prefix -> expiry in days, for the prefixes that have a retention configured."""
    configured = {
        settings.minio_source_prefix: settings.minio_source_retention_days,
        settings.minio_output_prefix: settings.minio_output_retention_days,
    }
    return {prefix: days for prefix, days in configured.items() if prefix and days > 0}
