"""Storage backends for persisting uploaded media (local disk or S3)."""
from services.storage.base import StorageBackend, StoredFile, get_storage

__all__ = ["StorageBackend", "StoredFile", "get_storage"]
