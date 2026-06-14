"""
services/signal_service.py — إشارات بدون AI

يحسب من الـ segments مباشرة:
- وتيرة الكلام (كلمة/دقيقة)
- الصمت الأطول
- أطول monologue
- Keyword detection (كلمات خطر وفرصة ومنافسين)

كل ده بدون AI → سريع ومجاني.
"""
import re
from dataclasses import dataclass, field


# ── قوائم الكلمات ──────────────────────────────────
DANGER_KEYWORDS = [
    # سعر وميزانية
    r"\b(غالي|غالية|مكلف|مكلفة|مش قادر|مش قادرين|تكلفة عالية|ميزانية محدودة|مفيش ميزانية)\b",
    # تردد وتأجيل
    r"\b(محتاج وقت|هنفكر|هنشوف|مش متأكد|مش عارف|لازم أستشير|مش الوقت المناسب)\b",
    r"\b(نرجع ليك|هنتواصل معاك|تاني مرة|مش دلوقتي|مستعجلش)\b",
    # رفض
    r"\b(مش مهتم|مش محتاج|عندنا حل|مش مناسب)\b",
]

OPPORTUNITY_KEYWORDS = [
    # شراء
    r"\b(عايز أجرب|عايزين نجرب|مهتم|مهتمين|نبدأ|نتفق|نوقّع|امتى نبدأ)\b",
    r"\b(بكام|السعر إيه|إيه الخطوة الجاية|نعمل ايه دلوقتي)\b",
    # إيجابية
    r"\b(ممتاز|رائع|كويس جداً|يناسبنا|ده اللي بندور عليه|مظبوط)\b",
    r"\b(عايز تفاصيل|أعرف أكتر|احتاج عرض|ورّينا)\b",
]

HESITATION_MARKERS = [
    r"\b(يعني|يعني يعني|هممم|آه آه|بصراحة|خليني أشوف|إيه يعني)\b",
]


@dataclass
class MeetingSignals:
    # وتيرة الكلام
    rep_speaking_pace_wpm:   float = 0.0   # كلمة / دقيقة للمندوب
    cust_speaking_pace_wpm:  float = 0.0   # كلمة / دقيقة للعميل

    # الصمت
    longest_silence_sec:     float = 0.0   # أطول صمت
    avg_silence_sec:         float = 0.0   # متوسط الصمت
    silence_count:           int   = 0     # عدد فترات الصمت > 3 ثواني

    # المونولوج
    longest_monologue_sec:   float = 0.0   # أطول حديث متواصل للمندوب
    longest_monologue_text:  str   = ""    # نصه

    # التبادل
    interruption_count:      int   = 0     # تداخل (gap < 0)
    avg_response_time_sec:   float = 0.0   # متوسط وقت الرد

    # Keyword hits
    danger_hits:    list = field(default_factory=list)    # [{word, timestamp, text}]
    opportunity_hits: list = field(default_factory=list)
    hesitation_hits:  list = field(default_factory=list)

    # Summary counts
    danger_count:     int = 0
    opportunity_count: int = 0
    hesitation_count:  int = 0


def extract_signals(segments: list) -> MeetingSignals:
    """
    يستخرج كل الإشارات من قائمة الـ segments.
    segments: [{"speaker", "text", "start", "end", "duration", "word_count"}]
    """
    sig = MeetingSignals()

    if not segments:
        return sig

    # ── 1. وتيرة الكلام ──────────────────────────────
    rep_words   = sum(s["word_count"] for s in segments if s["speaker"] == "sales_rep")
    cust_words  = sum(s["word_count"] for s in segments if s["speaker"] == "customer")
    rep_time    = sum(s["duration"]   for s in segments if s["speaker"] == "sales_rep")
    cust_time   = sum(s["duration"]   for s in segments if s["speaker"] == "customer")

    sig.rep_speaking_pace_wpm  = round((rep_words  / rep_time  * 60) if rep_time  > 0 else 0, 1)
    sig.cust_speaking_pace_wpm = round((cust_words / cust_time * 60) if cust_time > 0 else 0, 1)

    # ── 2. الصمت بين الـ segments ─────────────────────
    silences = []
    for i in range(1, len(segments)):
        gap = segments[i]["start"] - segments[i-1]["end"]
        if gap > 0.5:  # أكثر من نص ثانية
            silences.append(gap)
        if gap < -0.2:  # تداخل
            sig.interruption_count += 1

    if silences:
        sig.longest_silence_sec = round(max(silences), 1)
        sig.avg_silence_sec     = round(sum(silences) / len(silences), 1)
        sig.silence_count       = sum(1 for s in silences if s > 3)

    # ── 3. أطول monologue للمندوب ──────────────────────
    current_mono_secs = 0.0
    current_mono_text = []
    best_mono_secs    = 0.0
    best_mono_text    = ""

    for seg in segments:
        if seg["speaker"] == "sales_rep":
            current_mono_secs += seg["duration"]
            current_mono_text.append(seg["text"])
        else:
            if current_mono_secs > best_mono_secs:
                best_mono_secs = current_mono_secs
                best_mono_text = " ".join(current_mono_text)
            current_mono_secs = 0.0
            current_mono_text = []

    # آخر mono لو انتهى الاجتماع بكلام المندوب
    if current_mono_secs > best_mono_secs:
        best_mono_secs = current_mono_secs
        best_mono_text = " ".join(current_mono_text)

    sig.longest_monologue_sec  = round(best_mono_secs, 1)
    sig.longest_monologue_text = best_mono_text[:200]  # أول 200 حرف

    # ── 4. متوسط وقت الرد ─────────────────────────────
    response_times = []
    for i in range(1, len(segments)):
        gap = segments[i]["start"] - segments[i-1]["end"]
        if 0 < gap < 10:  # رد طبيعي
            response_times.append(gap)

    sig.avg_response_time_sec = round(
        sum(response_times) / len(response_times), 1
    ) if response_times else 0.0

    # ── 5. Keyword detection ───────────────────────────
    full_text = " ".join(s["text"] for s in segments)

    # بنبني timestamp lookup
    def find_timestamp(keyword: str) -> float:
        """يجيب timestamp أول ظهور للكلمة."""
        for seg in segments:
            if re.search(keyword, seg["text"], re.IGNORECASE):
                return seg["start"]
        return 0.0

    def find_hits(patterns: list, category: str) -> list:
        hits = []
        for pattern in patterns:
            for seg in segments:
                matches = re.findall(pattern, seg["text"], re.IGNORECASE)
                for match in matches:
                    hits.append({
                        "word":      match if isinstance(match, str) else match[0],
                        "timestamp": round(seg["start"], 1),
                        "text":      seg["text"][:100],
                        "speaker":   seg["speaker"],
                        "category":  category,
                    })
        return hits

    sig.danger_hits      = find_hits(DANGER_KEYWORDS,     "danger")
    sig.opportunity_hits = find_hits(OPPORTUNITY_KEYWORDS, "opportunity")
    sig.hesitation_hits  = find_hits(HESITATION_MARKERS,  "hesitation")

    sig.danger_count      = len(sig.danger_hits)
    sig.opportunity_count = len(sig.opportunity_hits)
    sig.hesitation_count  = len(sig.hesitation_hits)

    return sig


def signals_to_dict(sig: MeetingSignals) -> dict:
    return {
        "rep_speaking_pace_wpm":   sig.rep_speaking_pace_wpm,
        "cust_speaking_pace_wpm":  sig.cust_speaking_pace_wpm,
        "longest_silence_sec":     sig.longest_silence_sec,
        "avg_silence_sec":         sig.avg_silence_sec,
        "silence_count":           sig.silence_count,
        "longest_monologue_sec":   sig.longest_monologue_sec,
        "longest_monologue_text":  sig.longest_monologue_text,
        "interruption_count":      sig.interruption_count,
        "avg_response_time_sec":   sig.avg_response_time_sec,
        "danger_count":            sig.danger_count,
        "opportunity_count":       sig.opportunity_count,
        "hesitation_count":        sig.hesitation_count,
        "danger_hits":             sig.danger_hits,
        "opportunity_hits":        sig.opportunity_hits,
        "hesitation_hits":         sig.hesitation_hits,
    }
