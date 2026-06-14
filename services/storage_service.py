"""
services/storage_service.py — رفع وتخزين الملفات
- Cloudflare R2 لو الـ credentials موجودة
- Local storage كـ fallback
- Streaming upload (مش تحميل كامل في RAM)
"""
import os
import uuid
import shutil
import boto3
from pathlib import Path
from botocore.client import Config
from ..config import settings


class StorageService:

    def __init__(self):
        self._r2 = None

    def _get_r2(self):
        if self._r2 is None:
            self._r2 = boto3.client(
                "s3",
                endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                config=Config(signature_version="s3v4"),
                region_name="auto",
            )
        return self._r2

    def save_from_path(self, local_path: str, original_filename: str) -> str:
        """
        يرفع ملف موجود على القرص لـ R2 أو يحركه للمجلد الدائم.
        يُستخدم بعد الـ streaming upload.
        """
        ext      = Path(original_filename).suffix.lower()
        file_key = f"meetings/{uuid.uuid4()}{ext}"

        if settings.use_r2_storage:
            self._upload_to_r2(local_path, file_key)
            self.cleanup_temp(local_path)
            return file_key
        else:
            return self._move_to_local(local_path, file_key)

    def _upload_to_r2(self, local_path: str, key: str):
        """Streaming upload لـ R2."""
        file_size = os.path.getsize(local_path)
        r2 = self._get_r2()

        with open(local_path, "rb") as f:
            r2.upload_fileobj(
                f,
                settings.R2_BUCKET_NAME,
                key,
                ExtraArgs={"ContentLength": file_size},
            )
        print(f"☁️ Uploaded to R2: {key}")

    def _move_to_local(self, local_path: str, file_key: str) -> str:
        """نقل لمجلد التخزين المحلي الدائم."""
        dest = Path(settings.LOCAL_STORAGE_PATH) / file_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(local_path, dest)
        print(f"💾 Saved locally: {dest}")
        return file_key

    def get_local_path(self, file_key: str) -> str:
        """الحصول على المسار المحلي للملف."""
        if settings.use_r2_storage:
            # حمّل من R2 للمعالجة
            local_path = Path(settings.LOCAL_STORAGE_PATH) / "tmp" / Path(file_key).name
            local_path.parent.mkdir(parents=True, exist_ok=True)
            self._get_r2().download_file(settings.R2_BUCKET_NAME, file_key, str(local_path))
            return str(local_path)
        else:
            return str(Path(settings.LOCAL_STORAGE_PATH) / file_key)

    def delete(self, file_key: str):
        """حذف ملف من التخزين."""
        try:
            if settings.use_r2_storage:
                self._get_r2().delete_object(Bucket=settings.R2_BUCKET_NAME, Key=file_key)
            else:
                path = Path(settings.LOCAL_STORAGE_PATH) / file_key
                if path.exists():
                    path.unlink()
        except Exception as e:
            print(f"⚠️ Delete failed for {file_key}: {e}")

    def cleanup_temp(self, path: str):
        """حذف ملف مؤقت."""
        try:
            p = Path(path)
            if p.exists():
                p.unlink()
        except Exception:
            pass


storage_service = StorageService()
