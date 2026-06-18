"""
Module: Local Filesystem Storage
Purpose: Persists uploaded media to a local directory. Intended for development
         and single-node deployments.
"""
from __future__ import annotations

import os
import shutil
from typing import BinaryIO

from config.setting import get_settings
from services.storage.base import StoredFile


class LocalStorage:
    def __init__(self) -> None:
        self._dir = get_settings().LOCAL_STORAGE_DIR
        os.makedirs(self._dir, exist_ok=True)

    def save(self, file_id: str, fileobj: BinaryIO, content_type: str | None) -> StoredFile:
        dest = os.path.join(self._dir, file_id)
        fileobj.seek(0)
        with open(dest, "wb") as out:
            shutil.copyfileobj(fileobj, out, length=1024 * 1024)
        size = os.path.getsize(dest)
        return StoredFile(
            file_id=file_id,
            url=f"/media/{file_id}",
            backend="local",
            size=size,
        )
