"""
services/whisper_service.py — تحويل الصوت لنص بـ Whisper
- GPU تلقائي لو موجود
- Chunking للملفات الكبيرة
- تحديد المتكلمين بمنطق محسّن
"""
import os
import time
import threading
import torch
import whisper
from ..config import settings


class WhisperService:
    _model = None
    _backend = None  # "openai" أو "faster" — يتحدد فعلياً وقت التحميل
    # ── Thread safety ─────────────────────────────────
    # 1) _load_lock: يمنع تحميل الـ model مرتين لو أكتر من thread
    #    وصلوا لـ get_model() في نفس اللحظة (double VRAM → CUDA OOM).
    # 2) _infer_lock: openai-whisper بيركّب KV-cache hooks على الـ model
    #    أثناء الـ decode → استدعاء transcribe() من threads متوازية على
    #    نفس الـ model بيبوّظ النتائج. الـ lock بيخلي الـ inference متسلسل
    #    (وده مفيش فيه خسارة فعلية — GPU واحد بيسلسل الـ kernels أصلاً).
    _load_lock  = threading.Lock()
    _infer_lock = threading.Lock()

    @classmethod
    def get_model(cls):
        """تحميل الـ model مرة واحدة فقط (singleton + double-checked locking).

        ✅ PERF: لو WHISPER_BACKEND=faster و faster-whisper متثبتة →
        CTranslate2 backend: أسرع 3-5× وأقل VRAM بنحو 40-60% بنفس الدقة.
        لو المكتبة مش متثبتة → fallback تلقائي لـ openai-whisper مع تحذير.
        """
        if cls._model is None:
            with cls._load_lock:
                if cls._model is None:  # re-check بعد الحصول على الـ lock
                    device = cls._detect_device()

                    if settings.WHISPER_BACKEND == "faster":
                        try:
                            from faster_whisper import WhisperModel as FasterWhisperModel
                            compute = settings.WHISPER_COMPUTE_TYPE or (
                                "int8_float16" if device == "cuda" else "int8"
                            )
                            # faster-whisper لا يدعم mps — نرجع cpu
                            fw_device = device if device in ("cuda", "cpu") else "cpu"
                            print(f"⏳ Loading faster-whisper {settings.WHISPER_MODEL} "
                                  f"on {fw_device} ({compute})...")
                            cls._model = FasterWhisperModel(
                                settings.WHISPER_MODEL,
                                device=fw_device,
                                compute_type=compute,
                                download_root=os.path.expanduser("~/.whisper"),
                            )
                            cls._backend = "faster"
                            print(f"✅ faster-whisper loaded on {fw_device}")
                            return cls._model
                        except ImportError:
                            print("⚠️ WHISPER_BACKEND=faster لكن faster-whisper مش متثبتة "
                                  "(pip install faster-whisper) — fallback لـ openai-whisper")

                    print(f"⏳ Loading Whisper {settings.WHISPER_MODEL} on {device}...")
                    cls._model = whisper.load_model(
                        settings.WHISPER_MODEL,
                        device=device,
                        download_root=os.path.expanduser("~/.whisper"),
                    )
                    cls._backend = "openai"
                    print(f"✅ Whisper loaded on {device}")
        return cls._model

    @classmethod
    def _detect_device(cls) -> str:
        if settings.WHISPER_DEVICE == "cuda" and torch.cuda.is_available():
            print(f"🎮 GPU: {torch.cuda.get_device_name(0)}")
            return "cuda"
        if settings.WHISPER_DEVICE == "mps" and torch.backends.mps.is_available():
            return "mps"
        print("💻 Using CPU (slower)")
        return "cpu"

    def transcribe(self, audio_path: str, previous_text: str = "") -> dict:
        """
        تحويل ملف صوتي لنص مع تحديد المتكلمين.
        previous_text: آخر جملتين من الـ chunk السابق للسياق.
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio not found: {audio_path}")

        model = self.get_model()
        start = time.time()

        base_prompt = "هذا تسجيل اجتماع مبيعات باللغة العربية."
        if previous_text:
            context        = previous_text.strip()[-200:]
            initial_prompt = f"{base_prompt} {context}"
        else:
            initial_prompt = base_prompt

        print(f"🎙️ Transcribing: {os.path.basename(audio_path)}")

        # inference متسلسل — كلا الـ backends مش thread-safe على نفس الـ model
        with self._infer_lock:
            if self._backend == "faster":
                raw_segments = self._transcribe_faster(model, audio_path, initial_prompt)
                raw_text     = " ".join(s["text"] for s in raw_segments)
                result       = {"text": raw_text, "segments": raw_segments,
                                "language": settings.WHISPER_LANGUAGE}
            else:
                result = model.transcribe(
                    audio_path,
                    language=settings.WHISPER_LANGUAGE,
                    task="transcribe",
                    temperature=0.0,
                    compression_ratio_threshold=2.4,
                    no_speech_threshold=0.6,
                    condition_on_previous_text=True,
                    word_timestamps=True,
                    verbose=False,
                    initial_prompt=initial_prompt,
                )

        processing_time = int(time.time() - start)
        clean_text      = self._clean_text(result["text"])
        segments        = self._diarize_segments(result["segments"])
        talk_ratio      = self._calc_talk_ratio(segments)

        print(f"✅ Done in {processing_time}s | {len(clean_text.split())} words | talk_ratio={talk_ratio}%")

        return {
            "text":            clean_text,
            "segments":        segments,
            "language":        result.get("language", "ar"),
            "processing_time": processing_time,
            "word_count":      len(clean_text.split()),
            "talk_ratio":      talk_ratio,
        }

    def _transcribe_faster(self, model, audio_path: str, initial_prompt: str) -> list:
        """
        faster-whisper بيرجع generator من segment objects —
        نحوّلها لنفس شكل dicts بتاع openai-whisper عشان باقي الـ pipeline
        (الـ diarization heuristics والـ chunking) يشتغل بدون أي تعديل.
        """
        segments_gen, _info = model.transcribe(
            audio_path,
            language=settings.WHISPER_LANGUAGE,
            task="transcribe",
            temperature=0.0,
            compression_ratio_threshold=2.4,
            no_speech_threshold=0.6,
            condition_on_previous_text=True,
            initial_prompt=initial_prompt,
            vad_filter=True,  # يتخطى الصمت الطويل — أسرع وأدق
        )
        return [
            {"start": float(s.start), "end": float(s.end), "text": s.text}
            for s in segments_gen
        ]

    def _clean_text(self, text: str) -> str:
        """تنظيف النص العربي."""
        import re
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'\.{3,}', '...', text)
        text = re.sub(r'(صح|أيوه|تمام|ماشي)\s*\1(\s*\1)*', r'\1', text)
        return text.strip()

    def _diarize_segments(self, raw_segments: list) -> list:
        """
        تحديد المتكلمين:
        - الجملة الأولى: المندوب (هو من فتح الاجتماع)
        - جملة قصيرة + سؤال: عميل
        - جملة طويلة أو شرح: مندوب
        - تبادل سريع (< 1.5s): عكس المتكلم السابق
        """
        if not raw_segments:
            return []

        identified = []
        prev_end   = 0.0

        for i, seg in enumerate(raw_segments):
            text       = seg.get("text", "").strip()
            # ✅ FIX: حوّل كل القيم numpy لـ Python float عادي
            start      = float(seg.get("start", 0))
            end        = float(seg.get("end", 0))
            duration   = end - start
            word_count = len(text.split())

            is_question         = text.strip().endswith(("?", "؟"))
            is_very_short       = word_count < 8
            is_long_explanation = word_count > 25
            gap_from_prev       = start - prev_end

            if i == 0:
                speaker = "sales_rep"
            elif is_question and is_very_short:
                speaker = "customer"
            elif is_long_explanation:
                speaker = "sales_rep"
            elif gap_from_prev < 1.5 and identified:
                prev_speaker = identified[-1]["speaker"]
                speaker = "customer" if prev_speaker == "sales_rep" else "sales_rep"
            else:
                speaker = "sales_rep"

            identified.append({
                "speaker":    speaker,
                "text":       text,
                "start":      round(start, 2),    # ✅ الآن float عادي
                "end":        round(end, 2),       # ✅ الآن float عادي
                "duration":   round(duration, 2), # ✅ الآن float عادي
                "word_count": word_count,
            })
            prev_end = end

        return identified

    def _calc_talk_ratio(self, segments: list) -> float:
        """نسبة كلام المندوب من إجمالي وقت الكلام."""
        if not segments:
            return 50.0
        rep_time   = sum(s["duration"] for s in segments if s["speaker"] == "sales_rep")
        total_time = sum(s["duration"] for s in segments)
        if total_time == 0:
            return 50.0
        return round(float(rep_time / total_time) * 100, 1)  # ✅ float صريح


# Singleton
whisper_service = WhisperService()


def transcribe_audio(audio_path: str) -> dict:
    return whisper_service.transcribe(audio_path)