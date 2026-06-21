"""Storage backend for persisting uploaded media to S3-compatible object storage."""
from services.storage.base import StorageBackend, StoredFile, get_storage

__all__ = ["StorageBackend", "StoredFile", "get_storage"]
