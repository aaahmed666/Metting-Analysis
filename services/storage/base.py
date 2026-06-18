"""
Module: Storage Backend Abstraction
Purpose: Defines a common interface for persisting uploaded media and selects
         the concrete backend (local disk or S3) from application settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import BinaryIO, Protocol

from config.setting import get_settings


@dataclass
class StoredFile:
    file_id: str
    url: str
    backend: str
    size: int


class StorageBackend(Protocol):
    def save(self, file_id: str, fileobj: BinaryIO, content_type: str | None) -> StoredFile:
        ...


@lru_cache
def get_storage() -> StorageBackend:
    settings = get_settings()
    if settings.STORAGE_BACKEND == "s3":
        from services.storage.s3_backend import S3Storage
        return S3Storage()
    from services.storage.local_backend import LocalStorage
    return LocalStorage()
