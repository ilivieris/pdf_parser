from __future__ import annotations

from pathlib import Path
from threading import Lock

from document_processor_service.app.core.config import settings


class ObjectStorageError(RuntimeError):
    """Raised when the local file store returns an operational error."""


class ObjectNotFoundError(ObjectStorageError):
    """Raised when a file cannot be found on disk."""


class LocalFileStore:
    def __init__(self) -> None:
        self._root = Path(settings.document_storage_root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, *, key: str, data: bytes) -> str:
        target = (self._root / key).resolve()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise ObjectStorageError("Local storage write failed.") from exc
        return str(target)

    def get_bytes(self, path: str) -> bytes:
        candidate = Path(path).expanduser()
        resolved = candidate if candidate.is_absolute() else (self._root / candidate)
        try:
            return resolved.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(f"File not found: {resolved}") from exc
        except OSError as exc:
            raise ObjectStorageError("Local storage read failed.") from exc


_store: LocalFileStore | None = None
_store_lock = Lock()


def get_local_store() -> LocalFileStore:
    global _store
    if _store is not None:
        return _store

    with _store_lock:
        if _store is None:
            _store = LocalFileStore()
        return _store
