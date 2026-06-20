"""
Module: S3 / Cloud Storage
Purpose: Persists uploaded media to an S3 (or S3-compatible) bucket and returns
         a retrievable URL for the stored object.
"""
from __future__ import annotations

import os
from typing import BinaryIO

import boto3

from config.setting import get_settings
from services.storage.base import StoredFile


class S3Storage:
    def __init__(self) -> None:
        s = get_settings()
        if not s.S3_BUCKET:
            raise RuntimeError("STORAGE_BACKEND=s3 but S3_BUCKET is not configured.")
        self._settings = s
        self._client = boto3.client(
            "s3",
            region_name=s.S3_REGION,
            endpoint_url=s.S3_ENDPOINT_URL,
            aws_access_key_id=(
                s.AWS_ACCESS_KEY_ID.get_secret_value() if s.AWS_ACCESS_KEY_ID else None
            ),
            aws_secret_access_key=(
                s.AWS_SECRET_ACCESS_KEY.get_secret_value()
                if s.AWS_SECRET_ACCESS_KEY
                else None
            ),
        )

    def save(self, file_id: str, fileobj: BinaryIO, content_type: str | None) -> StoredFile:
        s = self._settings
        key = f"uploads/{file_id}"
        fileobj.seek(0, os.SEEK_END)
        size = fileobj.tell()
        fileobj.seek(0)

        extra = {"ContentType": content_type} if content_type else {}
        self._client.upload_fileobj(fileobj, s.S3_BUCKET, key, ExtraArgs=extra)

        if s.S3_PUBLIC_BASE_URL:
            url = f"{s.S3_PUBLIC_BASE_URL.rstrip('/')}/{key}"
        elif s.S3_ENDPOINT_URL:
            url = f"{s.S3_ENDPOINT_URL.rstrip('/')}/{s.S3_BUCKET}/{key}"
        else:
            url = f"https://{s.S3_BUCKET}.s3.{s.S3_REGION}.amazonaws.com/{key}"

        return StoredFile(file_id=file_id, url=url, backend="s3", size=size)
