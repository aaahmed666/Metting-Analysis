"""
services/chunking_service.py

تقسيم الصوت لـ chunks ومعالجتها بشكل parallel ثم الدمج.

الفكرة:
- اجتماع 40 دقيقة → 8 chunks × 5 دقائق
- كل chunk يتحول لنص مستقل
- الـ segments بتاخد offset تلقائي (chunk 2 يبدأ من 300s مش 0s)
- النص النهائي = دمج كل النصوص مع تنظيف الحدود
- التحليل AI يشتغل على النص الكامل المدموج

المزايا:
- أسرع بكتير على GPU (parallel)
- لو chunk واحد فشل → بنعيد chunk ده بس
- ذاكرة أقل (GPU مش بتشيل كل الاجتماع)
- progress tracking دقيق (chunk 3/8 مش "جاري...")
"""
import os
import time
import subprocess
import tempfile
import concurrent.futures
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

from .whisper_service import whisper_service
from ..config import settings


@dataclass
class AudioChunk:
    index: int           # ترتيب الـ chunk (0-based)
    path: str            # مسار الملف المؤقت
    start_sec: float     # بداية الـ chunk في الاجتماع الأصلي
    end_sec: float       # نهاية الـ chunk
    duration_sec: float  # مدة الـ chunk


@dataclass
class ChunkTranscript:
    index: int
    text: str
    segments: list       # مع التوقيت المعدّل بـ offset
    start_sec: float
    end_sec: float       # نهاية الـ chunk في الاجتماع الأصلي
    processing_time: float
    success: bool
    error: Optional[str] = None


class ChunkingService:
    """
    يقسم الصوت لـ chunks ويعالجها بشكل parallel.
    """

    def split_audio(
        self,
        audio_path: str,
        chunk_duration_sec: int = 300,  # 5 دقائق افتراضي
        overlap_sec: int = 10,          # 10 ثانية overlap بين chunks
    ) -> list[AudioChunk]:
        """
        تقسيم ملف صوتي لـ chunks متساوية.

        overlap_sec مهم جداً:
        - لو جملة بدأت في آخر chunk وخلصت في الأول → مش بتتقطع
        - بنتجاهل الـ overlap عند الدمج (آخر overlap_sec من كل chunk)
        """
        total_duration = self._get_duration(audio_path)
        if total_duration == 0:
            raise ValueError(f"Cannot get duration: {audio_path}")

        chunks = []
        chunk_dir = tempfile.mkdtemp(prefix="si_chunks_")
        chunk_idx = 0
        start = 0.0

        while start < total_duration:
            end = min(start + chunk_duration_sec + overlap_sec, total_duration)
            chunk_path = os.path.join(chunk_dir, f"chunk_{chunk_idx:03d}.wav")

            self._cut_audio(audio_path, chunk_path, start, end)

            chunks.append(AudioChunk(
                index=chunk_idx,
                path=chunk_path,
                start_sec=start,
                end_sec=end,
                duration_sec=end - start,
            ))

            # الـ chunk التالي يبدأ بعد chunk_duration (مش بعد end)
            start += chunk_duration_sec
            chunk_idx += 1

        print(f"✂️  Split into {len(chunks)} chunks of ~{chunk_duration_sec//60}min "
              f"(overlap={overlap_sec}s) | total={total_duration:.0f}s")
        return chunks

    def transcribe_chunks(
        self,
        chunks: list[AudioChunk],
        max_workers: int = 1,
        progress_callback=None,
    ) -> list[ChunkTranscript]:
        """
        تحويل كل الـ chunks لنص.

        ✅ FIX (context race): النسخة القديمة كانت بتقرأ previous_texts[i]
        لحظة بداية الـ chunk، بس الكتابة كانت بتحصل لحظة *انتهاء* الـ chunk
        السابق — وكل chunks الـ batch بتتقدّم في نفس اللحظة → نص الـ context
        كان فاضي لنص الـ chunks. دلوقتي:
        - max_workers == 1 (الافتراضي): معالجة متسلسلة بسلسلة context كاملة
          وصحيحة 100% (وده الصح على GPU واحد — الـ inference متسلسل بـ lock
          في WhisperService أصلاً فمفيش خسارة سرعة).
        - max_workers > 1: batches متوازية، والـ context يُمرَّر *كقيمة* وقت
          الـ submit — أول chunk في كل batch ياخد آخر كلمات آخر chunk ناجح
          من الـ batch السابق (مكتمل ومضمون)، والباقي بدون context.
          مفيش قراءة/كتابة متشاركة → مفيش race.
        """
        results: list[Optional[ChunkTranscript]] = [None] * len(chunks)

        def process_chunk(chunk: AudioChunk, prev_text: str) -> ChunkTranscript:
            t0 = time.time()
            try:
                print(f"  🎙️ Chunk {chunk.index+1}/{len(chunks)} "
                      f"[{chunk.start_sec:.0f}s → {chunk.end_sec:.0f}s]")

                result = whisper_service.transcribe(chunk.path, previous_text=prev_text)

                # تعديل timing كل segment بـ offset
                adjusted_segments = []
                for seg in result.get("segments", []):
                    adjusted_segments.append({
                        **seg,
                        "start": seg["start"] + chunk.start_sec,
                        "end":   seg["end"]   + chunk.start_sec,
                    })

                return ChunkTranscript(
                    index=chunk.index,
                    text=result["text"],
                    segments=adjusted_segments,
                    start_sec=chunk.start_sec,
                    end_sec=chunk.end_sec,
                    processing_time=time.time() - t0,
                    success=True,
                )

            except Exception as e:
                print(f"  ❌ Chunk {chunk.index+1} failed: {e}")
                return ChunkTranscript(
                    index=chunk.index,
                    text="",
                    segments=[],
                    start_sec=chunk.start_sec,
                    end_sec=chunk.end_sec,
                    processing_time=time.time() - t0,
                    success=False,
                    error=str(e),
                )

        def _tail_words(text: str, n: int = 30) -> str:
            """آخر n كلمة من نص — تُستخدم كـ context للـ chunk التالي."""
            return " ".join(text.split()[-n:])

        completed = 0

        # ── المسار المتسلسل (الافتراضي والموصى به على GPU واحد) ──
        if max_workers <= 1:
            prev_context = ""
            for chunk in chunks:
                result = process_chunk(chunk, prev_context)
                results[chunk.index] = result
                if result.success and result.text:
                    prev_context = _tail_words(result.text)
                completed += 1
                if progress_callback:
                    progress_callback(completed, len(chunks))
                print(f"  ✅ Chunk {result.index+1} done in {result.processing_time:.1f}s")
            return results

        # ── المسار المتوازي (multi-GPU setups فقط) ──
        batch_size = max_workers
        for batch_start in range(0, len(chunks), batch_size):
            batch = chunks[batch_start: batch_start + batch_size]

            # الـ context يُحسب *قبل* الـ submit من نتائج الـ batch السابق
            # (مكتملة ومضمونة لأن الـ executor السابق اتعمل له join).
            prev_context = ""
            if batch_start > 0:
                prev_result = results[batch_start - 1]
                if prev_result and prev_result.success and prev_result.text:
                    prev_context = _tail_words(prev_result.text)

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {}
                for i, chunk in enumerate(batch):
                    # أول chunk في الـ batch بس هو اللي عنده context مضمون
                    ctx = prev_context if i == 0 else ""
                    futures[executor.submit(process_chunk, chunk, ctx)] = chunk

                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    results[result.index] = result
                    completed += 1
                    if progress_callback:
                        progress_callback(completed, len(chunks))
                    print(f"  ✅ Chunk {result.index+1} done in {result.processing_time:.1f}s")

        return results

    def merge_results(
        self,
        chunk_transcripts: list[ChunkTranscript],
        overlap_sec: int = 10,
    ) -> dict:
        """
        دمج نتائج كل الـ chunks في نتيجة واحدة.

        التعامل مع الـ overlap:
        - نحذف آخر overlap_sec من نص كل chunk عند الدمج
        - الـ segments بتتصفى بنفس المنطق

        التعامل مع الـ chunks الفاشلة:
        - بنضيف [لم يتم تحويل هذا الجزء] بدل النص المفقود
        """
        # ترتيب حسب index
        sorted_chunks = sorted(chunk_transcripts, key=lambda c: c.index)

        all_texts = []
        all_segments = []
        total_processing = 0
        failed_chunks = []

        for i, chunk in enumerate(sorted_chunks):
            total_processing += chunk.processing_time

            if not chunk.success:
                failed_chunks.append(chunk.index + 1)
                all_texts.append(f"\n[لم يتم تحويل هذا الجزء - خطأ في chunk {chunk.index+1}]\n")
                continue

            # حساب نهاية الـ chunk الفعلية (بدون overlap مع التالي)
            is_last_chunk = (i == len(sorted_chunks) - 1)
            effective_end = chunk.end_sec if is_last_chunk else (chunk.end_sec - overlap_sec)

            # فلترة الـ segments ضمن النطاق الفعلي
            valid_segments = [
                seg for seg in chunk.segments
                if seg["start"] >= chunk.start_sec and seg["end"] <= effective_end + 1
            ]

            # ✅ FIX (overlap duplication): النص بيتبني من الـ segments
            # *المصفّاة* (اللي الـ overlap اتشال منها فعلاً) — مش من
            # chunk.text الخام اللي كان بيحتوي على آخر 10 ثواني مكررة
            # من كل حدّ بين chunk والتالي.
            if valid_segments:
                chunk_text = " ".join(
                    s.get("text", "").strip() for s in valid_segments
                    if s.get("text", "").strip()
                )
            else:
                # fallback: chunk نجح بس من غير segments (نادر) — نستخدم النص الخام
                chunk_text = chunk.text.strip()

            if chunk_text:
                all_texts.append(chunk_text)

            all_segments.extend(valid_segments)

        # دمج النصوص مع مسافة بين الـ chunks
        merged_text = " ".join(t.strip() for t in all_texts if t.strip())

        # تنظيف نهائي
        merged_text = self._clean_merged_text(merged_text)

        # حساب talk_ratio من كل الـ segments
        talk_ratio = self._calc_talk_ratio(all_segments)

        result = {
            "text": merged_text,
            "segments": all_segments,
            "word_count": len(merged_text.split()),
            "processing_time": int(total_processing),
            "talk_ratio": talk_ratio,
            "chunks_total": len(sorted_chunks),
            "chunks_failed": failed_chunks,
            "language": "ar",
        }

        if failed_chunks:
            print(f"⚠️  {len(failed_chunks)} chunks failed: {failed_chunks}")
        print(f"✅ Merged: {result['word_count']} words | talk_ratio={talk_ratio}% | "
              f"{result['chunks_total']} chunks")

        return result

    def transcribe_with_chunking(
        self,
        audio_path: str,
        chunk_duration_sec: int = 300,
        overlap_sec: int = 10,
        max_workers: int = 1,
        progress_callback=None,
    ) -> dict:
        """
        الـ entry point الرئيسي:
        split → transcribe → merge

        progress_callback: function(done, total) للـ status updates
        """
        print(f"\n📼 Chunked transcription: {audio_path}")
        t_total = time.time()

        # 1. تقسيم
        chunks = self.split_audio(audio_path, chunk_duration_sec, overlap_sec)

        # 2. معالجة parallel
        chunk_transcripts = self.transcribe_chunks(
            chunks, max_workers, progress_callback
        )

        # 3. دمج
        result = self.merge_results(chunk_transcripts, overlap_sec)
        result["total_elapsed"] = int(time.time() - t_total)

        # 4. تنظيف الـ chunk files
        self._cleanup_chunks(chunks)

        print(f"🏁 Total: {result['total_elapsed']}s for {result['chunks_total']} chunks")
        return result

    # =========================================
    # Helper Methods
    # =========================================

    def _get_duration(self, audio_path: str) -> float:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return float(r.stdout.strip())
        except Exception:
            return 0.0

    def _cut_audio(self, src: str, dst: str, start: float, end: float):
        """قطع جزء من الصوت بـ FFmpeg."""
        duration = end - start
        cmd = [
            "ffmpeg",
            "-ss", str(start),
            "-i", src,
            "-t", str(duration),
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            "-y", dst,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg cut failed: {result.stderr[:200]}")
        if not os.path.exists(dst) or os.path.getsize(dst) < 100:
            raise RuntimeError(f"Empty chunk created: {dst}")

    def _clean_merged_text(self, text: str) -> str:
        """تنظيف النص المدموج."""
        import re
        # إزالة مسافات متعددة
        text = re.sub(r'\s+', ' ', text)
        # إزالة تكرارات الكلمات عند حدود الـ chunks
        text = re.sub(r'\b(\w{3,})\s+\1\b', r'\1', text)
        # تنظيف علامات الترقيم المكررة
        text = re.sub(r'([.،؟!])\1+', r'\1', text)
        return text.strip()

    def _calc_talk_ratio(self, segments: list) -> float:
        if not segments:
            return 50.0
        rep_time = sum(s.get("duration", 0) for s in segments if s.get("speaker") == "sales_rep")
        total_time = sum(s.get("duration", 0) for s in segments)
        if total_time == 0:
            return 50.0
        return round((rep_time / total_time) * 100, 1)

    def _cleanup_chunks(self, chunks: list[AudioChunk]):
        """حذف ملفات الـ chunks المؤقتة."""
        for chunk in chunks:
            try:
                if os.path.exists(chunk.path):
                    os.remove(chunk.path)
            except Exception:
                pass
        # حذف الـ directory لو فاضية
        if chunks:
            chunk_dir = os.path.dirname(chunks[0].path)
            try:
                os.rmdir(chunk_dir)
            except Exception:
                pass


# Singleton
chunking_service = ChunkingService()
