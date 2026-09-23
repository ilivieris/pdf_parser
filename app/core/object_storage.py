from __future__ import annotations

import io
from datetime import timedelta
from threading import Lock
from urllib.parse import urlsplit

from minio import Minio
from minio.commonconfig import ENABLED, Filter
from minio.error import S3Error
from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

from document_processor_service.app.core.config import settings


class ObjectStorageError(RuntimeError):
    """Raised when the object store returns an operational error."""


class ObjectNotFoundError(ObjectStorageError):
    """Raised when an object cannot be found in the bucket."""


_NOT_FOUND_CODES = {"NoSuchKey", "NoSuchBucket", "NoSuchObject"}

# Prefix for the lifecycle rules this service owns. Rules on the bucket that do not start
# with it are left untouched, so a bucket shared with other workloads keeps its own policy.
_RULE_ID_PREFIX = "document-processor-"


def split_endpoint(endpoint: str, *, default_secure: bool) -> tuple[str, bool]:
    """Normalize `host:port` or `http(s)://host:port` into the (host:port, secure) the SDK wants."""
    endpoint = (endpoint or "").strip()
    if "://" not in endpoint:
        return endpoint, default_secure

    parsed = urlsplit(endpoint)
    return parsed.netloc, parsed.scheme == "https"


class MinioObjectStore:
    """Thin wrapper over the MinIO SDK: put, get, and presigned download links.

    The bucket is created on first use, so a fresh MinIO instance needs no manual setup.
    """

    def __init__(self) -> None:
        endpoint, secure = split_endpoint(settings.minio_endpoint, default_secure=settings.minio_secure)
        if not endpoint:
            raise ObjectStorageError("MINIO_ENDPOINT is not configured.")

        self._bucket = settings.minio_bucket
        self._client = Minio(
            endpoint,
            access_key=settings.minio_access_key or None,
            secret_key=settings.minio_secret_key or None,
            secure=secure,
            region=settings.minio_region or None,
        )

        # Presigned URLs are signed with SigV4, whose signature covers the Host header
        # (X-Amz-SignedHeaders=host) -- so a link cannot simply have its host swapped afterwards.
        # When the caller reaches MinIO under a different name than this service does (e.g. the
        # service dials `minio:9000` inside Compose while the browser needs `localhost:9000`),
        # the link has to be signed by a client bound to that public name from the start.
        self._presign_client = self._client
        public_endpoint, public_secure = split_endpoint(
            settings.minio_public_endpoint, default_secure=secure
        )
        if public_endpoint and (public_endpoint, public_secure) != (endpoint, secure):
            self._presign_client = Minio(
                public_endpoint,
                access_key=settings.minio_access_key or None,
                secret_key=settings.minio_secret_key or None,
                secure=public_secure,
                region=settings.minio_region or None,
            )

        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
        except S3Error as exc:
            # A bucket we can read but not list/create is fine -- only hard-fail if it is missing.
            if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists", "AccessDenied"}:
                raise ObjectStorageError(f"MinIO bucket check failed: {exc.code}") from exc
        except Exception as exc:
            raise ObjectStorageError("MinIO is unreachable.") from exc

    @property
    def bucket(self) -> str:
        return self._bucket

    def put_bytes(self, *, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        try:
            self._client.put_object(
                self._bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
        except Exception as exc:
            raise ObjectStorageError(f"MinIO write failed for '{key}'.") from exc
        return key

    def get_bytes(self, key: str) -> bytes:
        response = None
        try:
            response = self._client.get_object(self._bucket, key)
            return response.read()
        except S3Error as exc:
            if exc.code in _NOT_FOUND_CODES:
                raise ObjectNotFoundError(f"Object not found: {self._bucket}/{key}") from exc
            raise ObjectStorageError(f"MinIO read failed for '{key}'.") from exc
        except Exception as exc:
            raise ObjectStorageError(f"MinIO read failed for '{key}'.") from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def remove_object(self, key: str) -> None:
        """Delete an object. Missing objects are not an error -- the end state is the same."""
        try:
            self._client.remove_object(self._bucket, key)
        except S3Error as exc:
            if exc.code in _NOT_FOUND_CODES:
                return
            raise ObjectStorageError(f"MinIO delete failed for '{key}'.") from exc
        except Exception as exc:
            raise ObjectStorageError(f"MinIO delete failed for '{key}'.") from exc

    def ping(self) -> None:
        """Round-trip to the server, for the health check."""
        try:
            self._client.bucket_exists(self._bucket)
        except Exception as exc:
            raise ObjectStorageError(str(exc) or exc.__class__.__name__) from exc

    def apply_retention_policy(self, rules: dict[str, int]) -> None:
        """Expire objects under each prefix after the given number of days.

        Expiry is enforced by MinIO's own lifecycle scanner, not by this service, so it keeps
        working when the service is down and it does not break a download link that is still
        within its window. A prefix mapped to 0 days means "keep forever" and is passed in
        already filtered out by the caller.
        """
        existing = self._existing_foreign_rules()
        owned = [
            Rule(
                ENABLED,
                rule_filter=Filter(prefix=prefix),
                rule_id=f"{_RULE_ID_PREFIX}{prefix.strip('/').replace('/', '-') or 'root'}",
                expiration=Expiration(days=days),
            )
            for prefix, days in sorted(rules.items())
        ]

        try:
            if existing or owned:
                self._client.set_bucket_lifecycle(self._bucket, LifecycleConfig(existing + owned))
            else:
                self._client.delete_bucket_lifecycle(self._bucket)
        except Exception as exc:
            raise ObjectStorageError(f"MinIO lifecycle update failed: {exc}") from exc

    def _existing_foreign_rules(self) -> list[Rule]:
        """The bucket's current rules minus the ones this service owns, so ours can be replaced."""
        try:
            config = self._client.get_bucket_lifecycle(self._bucket)
        except S3Error as exc:
            if exc.code in _NOT_FOUND_CODES or exc.code == "NoSuchLifecycleConfiguration":
                return []
            raise ObjectStorageError(f"MinIO lifecycle read failed: {exc.code}") from exc
        except Exception as exc:
            raise ObjectStorageError(f"MinIO lifecycle read failed: {exc}") from exc

        if config is None:
            return []
        return [rule for rule in config.rules if not (rule.rule_id or "").startswith(_RULE_ID_PREFIX)]

    def presigned_url(self, key: str, *, expires_seconds: int | None = None) -> str:
        expiry = timedelta(seconds=expires_seconds or settings.minio_url_expiry_seconds)
        try:
            return self._presign_client.presigned_get_object(self._bucket, key, expires=expiry)
        except Exception as exc:
            raise ObjectStorageError(f"MinIO presign failed for '{key}'.") from exc


_store: MinioObjectStore | None = None
_store_lock = Lock()


def get_object_store() -> MinioObjectStore:
    global _store
    if _store is not None:
        return _store

    with _store_lock:
        if _store is None:
            _store = MinioObjectStore()
        return _store


def reset_object_store() -> None:
    """Drop the cached client so the next call reconnects (used by tests)."""
    global _store
    with _store_lock:
        _store = None
