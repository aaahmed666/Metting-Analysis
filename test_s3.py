"""
سكريبت تيست بسيط: بيختبر الاتصال بـ S3 وبيعمل رفع/قراءة/حذف ملف تجريبي.
بيقرا الإعدادات من ملف .env تلقائياً.

التشغيل:
    python3 test_s3.py
"""
import sys

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError, EndpointConnectionError
except ImportError:
    print("❌ boto3 مش متسطّب. شغّل: pip install boto3")
    sys.exit(1)

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    from pydantic import SecretStr
except ImportError:
    print("❌ pydantic-settings مش متسطّب. شغّل: pip install pydantic-settings")
    sys.exit(1)


class S3Test(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    S3_ENDPOINT_URL: str = "https://hel1.your-objectstorage.com"
    S3_REGION: str = "hel1"
    S3_BUCKET: str
    AWS_ACCESS_KEY_ID: SecretStr
    AWS_SECRET_ACCESS_KEY: SecretStr


def main():
    print("=" * 55)
    print("  اختبار الاتصال بـ S3 (Hetzner Object Storage)")
    print("=" * 55)

    # 1. قراءة الإعدادات
    try:
        cfg = S3Test()
    except Exception as e:
        print("\n❌ مشكلة في ملف .env — القيم ناقصة أو غلط:")
        print("  ", str(e).split("\n")[0])
        print("\n   اتأكد إن ملف .env فيه:")
        print("   S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY")
        print("   وإنه مش بالقيم الوهمية (your-access-key ...)")
        sys.exit(1)

    ak = cfg.AWS_ACCESS_KEY_ID.get_secret_value()
    sk = cfg.AWS_SECRET_ACCESS_KEY.get_secret_value()

    print(f"\n  Endpoint : {cfg.S3_ENDPOINT_URL}")
    print(f"  Region   : {cfg.S3_REGION}")
    print(f"  Bucket   : {cfg.S3_BUCKET}")
    print(f"  Access   : {ak}")

    # تحذير لو لسه بالقيم الوهمية
    if ak in ("your-access-key", "") or "your-" in ak:
        print("\n❌ المفاتيح لسه بالقيم الوهمية! حط المفاتيح الحقيقية في .env")
        sys.exit(1)

    client = boto3.client(
        "s3",
        endpoint_url=cfg.S3_ENDPOINT_URL,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        region_name=cfg.S3_REGION,
        config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=15),
    )

    key = "uploads/__healthcheck__"
    print("\n" + "-" * 55)

    # 2. رفع
    try:
        client.put_object(Bucket=cfg.S3_BUCKET, Key=key, Body=b"hello from test")
        print("  ✅ رفع ملف (PUT)      : نجح")
    except EndpointConnectionError:
        print("  ❌ مفيش اتصال بالـ endpoint — اتأكد من النت/الـ URL")
        sys.exit(1)
    except ClientError as e:
        code = e.response["Error"].get("Code")
        print(f"  ❌ رفع ملف (PUT)      : فشل — {code}")
        _explain(code, cfg.S3_BUCKET)
        sys.exit(1)

    # 3. قراءة
    try:
        body = client.get_object(Bucket=cfg.S3_BUCKET, Key=key)["Body"].read()
        print(f"  ✅ قراءة ملف (GET)    : نجح — {body!r}")
    except ClientError as e:
        print(f"  ❌ قراءة ملف (GET)    : فشل — {e.response['Error'].get('Code')}")
        sys.exit(1)

    # 4. حذف
    try:
        client.delete_object(Bucket=cfg.S3_BUCKET, Key=key)
        print("  ✅ حذف ملف (DELETE)   : نجح")
    except ClientError as e:
        print(f"  ⚠️  حذف ملف (DELETE)  : فشل — {e.response['Error'].get('Code')}")

    print("-" * 55)
    print("\n🎉 تمام! الـ S3 شغّال صح. المشروع جاهز يرفع على bucket "
          f"'{cfg.S3_BUCKET}'.\n")


def _explain(code, bucket):
    if code in ("403", "AccessDenied", "Forbidden"):
        print(f"\n   السبب: المفتاح ملوش صلاحية على bucket '{bucket}'.")
        print("   الحل: اعمل S3 credentials جديدة من جوّه نفس المشروع")
        print("        اللي فيه الـ bucket ده، في Hetzner Console:")
        print("        Security → S3 credentials → Generate credentials")
    elif code in ("404", "NoSuchBucket"):
        print(f"\n   السبب: bucket '{bucket}' مش موجود (الاسم غلط؟).")
        print("   الحل: راجع اسم الـ bucket من Hetzner Console.")
    elif code in ("SignatureDoesNotMatch", "InvalidAccessKeyId"):
        print("\n   السبب: المفاتيح نفسها غلط (access أو secret).")
        print("   الحل: راجع نسخهم من Hetzner Console.")


if __name__ == "__main__":
    main()
