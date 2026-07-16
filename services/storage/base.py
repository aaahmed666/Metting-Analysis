"""
Module: Storage Backend Abstraction
Purpose: Defines a common interface for persisting uploaded media to an
         S3-compatible object store.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import BinaryIO, Protocol


@dataclass
class StoredFile:
    file_id: str
    url: str
    backend: str
    size: int


class StorageBackend(Protocol):
    def save(self, file_id: str, fileobj: BinaryIO, content_type: str | None) -> StoredFile:
        ...

    def save_file(self, file_id: str, filepath: str, content_type: str | None) -> StoredFile:
        ...


@lru_cache
def get_storage() -> StorageBackend:
    from services.storage.s3_backend import S3Storage
    return S3Storage()
