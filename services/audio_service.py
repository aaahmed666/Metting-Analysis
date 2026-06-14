"""services/audio_service.py — استخراج الصوت بـ FFmpeg"""
import os
import subprocess
from pathlib import Path


def _probe_audio_format(file_path: str) -> dict:
    """يرجع codec/sample_rate/channels لأول audio stream (أو {} لو فشل)."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate,channels",
        "-of", "default=noprint_wrappers=1",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        info = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v
        return info
    except Exception:
        return {}


def extract_audio(video_path: str) -> str:
    """
    استخراج الصوت وتحويله لـ WAV 16kHz mono.
    هذا الإعداد مثالي لـ Whisper.
    Returns: مسار ملف الصوت

    ✅ PERF FIX: لو الملف أصلاً WAV PCM 16kHz mono (مرفوع جاهز أو ناتج
    معالجة سابقة) — نرجعه زي ما هو ونوفر تمريرة FFmpeg كاملة.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    fmt = _probe_audio_format(video_path)
    if (
        Path(video_path).suffix.lower() == ".wav"
        and fmt.get("codec_name") == "pcm_s16le"
        and fmt.get("sample_rate") == "16000"
        and fmt.get("channels") == "1"
    ):
        print(f"🎵 Audio already 16kHz mono WAV — skipping re-encode: {video_path}")
        return video_path

    audio_path = str(Path(video_path).stem) + "_audio.wav"
    audio_path = str(Path(video_path).parent / audio_path)

    cmd = [
        "ffmpeg", "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-af", "highpass=f=80,lowpass=f=8000,afftdn=nf=-25",
        "-y",
        audio_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        # جرّب بدون audio filter لو فشل
        cmd_simple = [
            "ffmpeg", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1",
            "-y", audio_path,
        ]
        result2 = subprocess.run(cmd_simple, capture_output=True, text=True, timeout=300)
        if result2.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr[:500]}")

    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise RuntimeError("FFmpeg produced empty audio file")

    size_mb = os.path.getsize(audio_path) / 1024 / 1024
    print(f"🎵 Audio extracted: {audio_path} ({size_mb:.1f}MB)")
    return audio_path


def get_duration_seconds(file_path: str) -> int:
    """استخراج مدة الملف بالثواني."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return int(float(result.stdout.strip()))
    except Exception:
        return 0
