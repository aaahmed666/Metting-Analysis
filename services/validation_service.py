"""
services/validation_service.py — التحقق من صحة الاجتماع
يكشف المحتوى غير الصالح (موسيقى، أفلام، محاضرات) ومحاولات الغش.
"""
import re
from dataclasses import dataclass
from ..config import settings


@dataclass
class ValidationResult:
    is_valid:         bool
    confidence:       float        # 0.0 → 1.0
    rejection_reason: str | None
    warning:          str | None
    signals:          dict


# محتوى غير صالح
NON_SALES_PATTERNS = [
    r"\b(كلمات الأغنية|song lyrics|♪|♫)\b",
    r"\b(مشهد \d+|scene \d+|فيلم|مسلسل|episode)\b",
    r"\b(الدرس|المحاضرة|الفصل الدراسي|الطلاب|الاختبار|quiz)\b",
    r"\b(مراسلنا|التقرير الإخباري|وكالة الأنباء)\b",
]

# علامات المحتوى التجاري
BUSINESS_PATTERNS = [
    r"\b(عرض|سعر|تكلفة|ميزانية|عقد|اتفاقية|خدمة|منتج|حل|نظام)\b",
    r"\b(مشتري|بائع|عميل|شركة|مندوب|اجتماع|عرض تقديمي)\b",
    r"\b(هل أنتم مهتمون|ما هي احتياجاتكم|نتفق|نوقع|نبدأ)\b",
    r"\b(ميزة|فائدة|عائد الاستثمار|ROI|توفير|ربح)\b",
]


class ValidationService:

    def validate(self, transcript: str, duration_seconds: int, word_count: int) -> ValidationResult:
        """
        يتحقق من صحة الاجتماع قبل التحليل.
        """
        signals = {}

        # ① فحص المدة
        duration_min = duration_seconds / 60
        if duration_min < 1:
            return ValidationResult(
                is_valid=False,
                confidence=0.99,
                rejection_reason="التسجيل أقصر من دقيقة واحدة — لا يمكن تحليله",
                warning=None,
                signals={"duration_min": round(duration_min, 2)},
            )

        signals["duration_min"] = round(duration_min, 2)

        # ② فحص عدد الكلمات
        if word_count < 50:
            return ValidationResult(
                is_valid=False,
                confidence=0.95,
                rejection_reason=f"النص قصير جداً ({word_count} كلمة) — لا يكفي للتحليل",
                warning=None,
                signals={"word_count": word_count},
            )

        signals["word_count"] = word_count
        text_lower = transcript.lower()

        # ③ فحص المحتوى غير الصالح
        for pattern in NON_SALES_PATTERNS:
            if re.search(pattern, transcript, re.IGNORECASE):
                return ValidationResult(
                    is_valid=False,
                    confidence=0.90,
                    rejection_reason="المحتوى لا يبدو أنه اجتماع مبيعات (قد يكون موسيقى، فيلم، أو محاضرة)",
                    warning=None,
                    signals={"matched_pattern": pattern},
                )

        # ④ فحص علامات المحتوى التجاري
        business_matches = sum(
            1 for pattern in BUSINESS_PATTERNS
            if re.search(pattern, transcript, re.IGNORECASE)
        )
        signals["business_signals"] = business_matches

        # تحذير لو محتوى تجاري قليل
        warning = None
        if business_matches < 1 and word_count > 200:
            warning = "المحتوى يحتوي على علامات تجارية قليلة — قد لا يكون اجتماع مبيعات"

        return ValidationResult(
            is_valid=True,
            confidence=min(0.5 + business_matches * 0.1, 0.99),
            rejection_reason=None,
            warning=warning,
            signals=signals,
        )


validation_service = ValidationService()
