"""
services/diarization_service.py — Speaker Diarization حقيقي (اختياري)
=====================================================================

المشكلة:
الـ heuristics في whisper_service._diarize_segments (طول الجملة / علامة استفهام /
الـ gap) مش diarization فعلي — مفيش embeddings ولا clustering، والـ default
بيرجّع sales_rep → الـ talk_ratio و25 نقطة الاستماع مبنيين على تخمين.

الحل:
- pyannote/speaker-diarization-3.1: يقسم الصوت لـ speaker clusters
  بـ embeddings + clustering حقيقي.
- بعدين نعمل mapping: كل Whisper segment ياخد الـ cluster اللي عنده
  أكبر تداخل زمني معاه.
- تحديد الأدوار (مين المندوب): الـ cluster بتاع أول segment = sales_rep
  (المندوب هو اللي بيفتح الاجتماع) — قابل للتطوير لاحقاً بـ voice profiles
  ثابتة لكل مندوب (ECAPA-TDNN embedding مخزّن عند الـ onboarding).

التفعيل:
1. pip install pyannote.audio
2. وافق على شروط pyannote/speaker-diarization-3.1 على HuggingFace
3. في .env:
   DIARIZATION_ENABLED=true
   HF_TOKEN=hf_xxxx

لو أي شرط ناقص → is_available() = False والـ pipeline يكمل
بالـ heuristics القديمة تلقائياً (zero downtime).
"""
import threading
from ..config import settings


class DiarizationService:
    _pipeline = None
    _load_lock = threading.Lock()
    _unavailable_reason = None

    @classmethod
    def is_available(cls) -> bool:
        """هل الـ diarization الحقيقي متاح؟ (config + dependency + token)"""
        if not settings.DIARIZATION_ENABLED:
            return False
        if not settings.HF_TOKEN:
            cls._unavailable_reason = "HF_TOKEN غير مضبوط"
            return False
        try:
            import pyannote.audio  # noqa: F401
        except ImportError:
            cls._unavailable_reason = "pyannote.audio غير مثبتة (pip install pyannote.audio)"
            return False
        return True

    @classmethod
    def get_pipeline(cls):
        """تحميل pipeline مرة واحدة (singleton + lock — نفس نمط WhisperService)."""
        if cls._pipeline is None:
            with cls._load_lock:
                if cls._pipeline is None:
                    from pyannote.audio import Pipeline
                    import torch

                    print("⏳ Loading pyannote speaker-diarization-3.1 ...")
                    pipeline = Pipeline.from_pretrained(
                        "pyannote/speaker-diarization-3.1",
                        use_auth_token=settings.HF_TOKEN,
                    )
                    if settings.WHISPER_DEVICE == "cuda" and torch.cuda.is_available():
                        pipeline.to(torch.device("cuda"))
                    cls._pipeline = pipeline
                    print("✅ pyannote loaded")
        return cls._pipeline

    def assign_roles(self, audio_path: str, segments: list) -> list:
        """
        يشغّل diarization على الصوت الكامل ويعيد تسمية speaker
        لكل segment بناءً على أكبر تداخل زمني مع الـ clusters.

        ملاحظة مهمة: بيشتغل على الصوت *الكامل* مرة واحدة (مش per-chunk) —
        عشان الـ clusters تفضل متسقة عبر الاجتماع كله.

        Returns: نفس الـ segments مع speaker محدّث + confidence حقيقي.
        لو فشل لأي سبب → يرجّع الـ segments زي ما هي (الـ heuristics باقية).
        """
        if not segments:
            return segments
        try:
            diarization = self.get_pipeline()(audio_path)
            turns = [
                (turn.start, turn.end, spk)
                for turn, _, spk in diarization.itertracks(yield_label=True)
            ]
            if not turns:
                return segments

            # ── mapping: كل segment → الـ cluster بأكبر تداخل ──
            for seg in segments:
                s, e = float(seg.get("start", 0)), float(seg.get("end", 0))
                seg_dur = max(e - s, 1e-6)
                overlaps: dict[str, float] = {}
                for t_start, t_end, spk in turns:
                    ov = min(e, t_end) - max(s, t_start)
                    if ov > 0:
                        overlaps[spk] = overlaps.get(spk, 0.0) + ov

                if overlaps:
                    best = max(overlaps, key=overlaps.get)
                    seg["_cluster"]   = best
                    seg["confidence"] = round(min(overlaps[best] / seg_dur, 1.0), 2)
                else:
                    seg["_cluster"]   = None
                    seg["confidence"] = 0.3  # مفيش تداخل — غالباً صمت/موسيقى

            # ── تحديد الأدوار: cluster أول segment له تداخل = المندوب ──
            rep_cluster = next(
                (seg["_cluster"] for seg in segments if seg.get("_cluster")),
                None,
            )
            for seg in segments:
                cluster = seg.pop("_cluster", None)
                if cluster is None:
                    continue  # سيب تسمية الـ heuristic
                seg["speaker"] = "sales_rep" if cluster == rep_cluster else "customer"

            n_clusters = len({t[2] for t in turns})
            print(f"🗣️ Diarization: {n_clusters} speakers detected | "
                  f"{len(turns)} turns mapped onto {len(segments)} segments")
            return segments

        except Exception as e:
            print(f"⚠️ Diarization failed — keeping heuristic labels: {e}")
            return segments


# Singleton
diarization_service = DiarizationService()
