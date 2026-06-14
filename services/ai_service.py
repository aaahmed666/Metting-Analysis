"""
services/ai_service.py — تحليل نص الاجتماع بالذكاء الاصطناعي

Features:
- Groq أولاً → Gemini كـ fallback
- Industry context مخصص لـ 8 قطاعات
- Sentiment trajectory: اهتمام العميل في 3 مراحل
- Opening script: جمل افتتاح جاهزة
- Competitor detection + رد مقترح
- Decision maker detection
"""
import json
import time
import httpx
from ..config import settings

# ── Industry Contexts ─────────────────────────────────
INDUSTRY_CONTEXTS: dict[str, str] = {
    "restaurant": """
القطاع: مطاعم وكافيهات
الاعتراضات الشائعة: تكلفة التركيب في المطبخ، خصوصية الموظفين، دقة كشف PPE، تعطّل الكاميرات أثناء الزحمة.
نقاط القيمة: food safety compliance، تقليل الهدر، drive-thru analytics، تتبع أوقات التحضير.
المنافسون الشائعون: أنظمة CCTV التقليدية، منصات مراقبة مطاعم أخرى.
السياق: اذكر حالات استخدام food safety وFoodics integration إن وُجدت.
""",
    "medical": """
القطاع: مستشفيات وعيادات
الاعتراضات الشائعة: خصوصية المرضى، امتثال HIPAA، رفض الكادر الطبي للمراقبة، تكامل مع أنظمة المشفى.
نقاط القيمة: مراقبة الطوارئ، تتبع الانتظار، PPE compliance للطاقم، restricted area monitoring.
السياق: الحساسية العالية لبيانات المرضى تتطلب تشفيراً وضمانات واضحة.
""",
    "factory": """
القطاع: مصانع ومستودعات
الاعتراضات الشائعة: تعطّل الإنتاج أثناء التركيب، دقة الكشف في بيئة التصنيع، اعتراض نقابة العمال.
نقاط القيمة: سلامة العمال (PPE)، كشف الحوادث، restricted area، overcrowd violations.
السياق: ركّز على ROI من تقليل حوادث العمل وتجنب الغرامات.
""",
    "retail": """
القطاع: مراكز تجارية ومحلات
الاعتراضات الشائعة: الميزانية، مقاومة إدارة المحل، جدوى البيانات مقابل التكلفة.
نقاط القيمة: people counting، heatmaps، queue management، تحسين تجربة العميل.
السياق: بيانات الـ footfall مرتبطة مباشرة بالمبيعات — هذا هو الـ ROI الأسهل للإثبات.
""",
    "parking": """
القطاع: مواقف السيارات
الاعتراضات الشائعة: دقة ALPR، تكلفة الكاميرات الخارجية، الظروف الجوية.
نقاط القيمة: license plate recognition، تتبع الإشغال، vehicle tracking، tailgating detection.
السياق: عائد الاستثمار واضح من تقليل التهرب وتحسين إدارة المواقف.
""",
    "education": """
القطاع: مدارس وجامعات
الاعتراضات الشائعة: خصوصية الطلاب، موافقة أولياء الأمور، ميزانية التعليم المحدودة.
نقاط القيمة: attendance monitoring، أمن المبنى، overcrowd في الممرات، face recognition.
السياق: التركيز على السلامة والحماية وليس المراقبة يساعد في تجاوز الاعتراضات.
""",
    "sports": """
القطاع: أندية رياضية وملاعب
الاعتراضات الشائعة: تكلفة الكاميرات في المساحات الكبيرة، التكامل مع أنظمة التذاكر.
نقاط القيمة: crowd management، إشغال المقاعد، أمن اللاعبين، تحليل حركة الجماهير.
السياق: الأندية الكبيرة لديها ميزانيات أكبر — ركّز على تجربة الجمهور والأمن.
""",
    "services": """
القطاع: شركات الخدمات الميدانية
الاعتراضات الشائعة: دقة التحقق من بُعد، تكلفة الأجهزة، اتصال الإنترنت في المواقع.
نقاط القيمة: توثيق تنفيذ المهام، PPE compliance، تتبع الأداء الميداني.
السياق: ركّز على تقليل النزاعات مع العملاء عبر التوثيق المرئي.
""",
}

# ── Scoring Criteria ──────────────────────────────────
SCORING_CRITERIA = """
معايير التقييم (اشرح كيف طبّقتها):

1. الاستماع (25 نقطة):
   - talk_ratio < 40%: ممتاز (23-25)
   - talk_ratio 40-55%: جيد (17-22)
   - talk_ratio 55-70%: مقبول (10-16)
   - talk_ratio > 70%: ضعيف (0-9)

2. جودة الاكتشاف (20 نقطة):
   - 3+ أسئلة مفتوحة: ممتاز (18-20)
   - 2: جيد (14-17) | 1: مقبول (8-13) | 0: ضعيف (0-7)

3. معالجة الاعتراضات (25 نقطة):
   - اعتراض معالج بامتياز: +8 (max 25)
   - عالج جيد: +5 | عالج ضعيف: +2 | لم يعالج: 0
   - لو مافيش اعتراضات: 20 نقطة افتراضية

4. الخطوات التالية (15 نقطة):
   - موعد محدد + مسؤول + خطوة واضحة: 15
   - موعد مبهم: 8 | لا شيء: 0

5. محاولة الإغلاق (15 نقطة):
   - طلب commitment صريح: 15
   - تلميح للإغلاق: 8
   - لم يحاول: 0
"""

ANALYSIS_PROMPT = """
أنت خبير تحليل مبيعات متخصص في السوق العربي.

معلومات الشركة:
- الشركة: {company_name}
- المنتج: {product_name}
- السوق المستهدف: {target_market}
- متوسط دورة المبيعات: {sales_cycle} يوم

{industry_context}

بيانات الاجتماع:
- العميل: {customer_name}
- مدة الاجتماع: {duration_minutes} دقيقة
- نسبة كلام المندوب: {talk_ratio}%
{signals_block}
النص الكامل للمحادثة:
=====================================
{transcript}
=====================================

{scoring_criteria}

أعطني التحليل الكامل بصيغة JSON فقط، بدون أي نص أو markdown خارجه:

{{
    "summary": "ملخص 3-4 جمل يصف ما حدث فعلاً في الاجتماع",

    "sentiment_trajectory": {{
        "opening": "hot|warm|neutral|cold",
        "middle":  "hot|warm|neutral|cold",
        "closing": "hot|warm|neutral|cold",
        "trend":   "improving|declining|stable|mixed",
        "turning_point": "وصف اللحظة اللي تغيّر فيها اهتمام العميل — أو null"
    }},

    "customer_questions": ["سؤال العميل الأول"],
    "customer_pain_points": ["مشكلة أو تحدٍ ذكره العميل"],
    "customer_interest": "high|medium|low",

    "decision_maker": {{
        "level": "owner|c_level|manager|supervisor|end_user",
        "confidence": "high|medium|low",
        "signals": ["إشارة 1 من النص تدعم هذا الحكم"],
        "recommendation": "ماذا يجب أن يفعل المندوب — مثلاً: اطلب اجتماع مع مدير العمليات"
    }},

    "competitor_intel": {{
        "mentioned": true,
        "names": ["اسم المنافس لو اتذكر"],
        "customer_reaction": "وصف موقف العميل من المنافس — أو null",
        "competitive_response": "الرد المقترح للمندوب — أو null"
    }},

    "objections": [
        {{
            "text": "نص الاعتراض",
            "category": "price|features|timing|trust|competition|support|other",
            "was_handled": true,
            "handling_quality": "excellent|good|poor|not_handled",
            "handling_notes": "كيف تعامل معه المندوب"
        }}
    ],

    "rep_strengths": ["نقطة قوة محددة مع مثال من المحادثة"],
    "rep_weaknesses": ["نقطة ضعف محددة مع تحسين مقترح"],
    "missing_topics": ["موضوع كان يجب يتكلم فيه"],
    "coaching_notes": "ملاحظات مفصّلة للمدير لمساعدة المندوب",

    "next_steps": ["خطوة تالية محددة"],
    "follow_up_days": 2,

    "opening_script": {{
        "line1": "أول جملة تفتح بيها المكالمة القادمة — محددة وشخصية بناءً على هذا الاجتماع",
        "line2": "الجملة الثانية — تذكير بالسياق أو طرح سؤال",
        "line3": "الجملة الثالثة — دعوة للمتابعة أو الخطوة التالية",
        "tip": "نصيحة واحدة قصيرة للمندوب قبل المكالمة"
    }},

    "closing_probability": 65,
    "deal_stage": "qualified|proposal|negotiation|closing|lost",

    "scores": {{
        "listening_score": 20,
        "discovery_score": 15,
        "objection_score": 18,
        "next_steps_score": 12,
        "closing_score": 10
    }}
}}
"""


def _call_groq(prompt: str) -> str:
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": settings.GROQ_MODEL,
                  "messages": [
                      {"role": "system", "content": "أنت محلل مبيعات خبير. ترد دائماً بـ JSON صحيح فقط، بدون أي نص خارج الـ JSON. اعتمد في كل حكم على أدلة من النص المعطى."},
                      {"role": "user", "content": prompt},
                  ],
                  "temperature": 0.1, "max_tokens": 4000,
                  "response_format": {"type": "json_object"}},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _call_gemini(prompt: str) -> str:
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("No Gemini API key")
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={settings.GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.1, "response_mime_type": "application/json"}},
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _parse_json_response(text: str) -> dict:
    import re
    text = re.sub(r'```json?\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    return json.loads(text.strip())


# ── Max score per component (للـ clamping) ────────────
_MAX_PER = {"listening_score": 25, "discovery_score": 20,
            "objection_score": 25, "next_steps_score": 15, "closing_score": 15}


def _clamp_scores(scores: dict) -> dict:
    """يضمن إن كل score ضمن النطاق الصحيح [0, max] — يمنع قيم AI خارج الحدود."""
    clamped = {}
    for key, max_val in _MAX_PER.items():
        try:
            val = float(scores.get(key, 0) or 0)
        except (TypeError, ValueError):
            val = 0.0
        clamped[key] = round(min(max(val, 0), max_val), 1)
    return clamped


def _window_transcript(transcript: str, max_chars: int = None) -> str:
    """
    ✅ FIX: القيمة القديمة (14000 حرف) كانت بتحذف 60-75% من اجتماع ساعة —
    وبالتحديد *المنتصف* حيث يحدث الـ discovery ومعالجة الاعتراضات
    (45 نقطة من الـ 100 بتتقيّم على نص الـ model ما شافوش).

    llama-3.3-70b سياقه 128K token؛ 48K حرف عربي ≈ 20-30K token فقط.
    الحد دلوقتي من الـ config (AI_MAX_TRANSCRIPT_CHARS) — قلّله من الـ .env
    لو اصطدمت بحدود TPM عند Groq.

    استراتيجية الـ overflow (للاجتماعات الأطول من الحد):
    بداية (45%) + عينة من المنتصف (15%) + نهاية (40%) —
    بدل head+tail فقط، عشان الاعتراضات في المنتصف ما تضيعش بالكامل.
    """
    max_chars  = max_chars or settings.AI_MAX_TRANSCRIPT_CHARS
    transcript = transcript.strip()
    if len(transcript) <= max_chars:
        return transcript

    head = transcript[: int(max_chars * 0.45)]
    mid_center = len(transcript) // 2
    mid_half   = int(max_chars * 0.15) // 2
    mid  = transcript[mid_center - mid_half: mid_center + mid_half]
    tail = transcript[-int(max_chars * 0.40):]

    return (
        f"{head}\n\n[... تم اختصار جزء من الاجتماع للطول ...]\n\n{mid}"
        f"\n\n[... تم اختصار جزء من الاجتماع للطول ...]\n\n{tail}"
    )


def _build_signals_block(signals: dict | None) -> str:
    """
    يحوّل الإشارات المحسوبة (بدون AI) لنص أدلة يُغذّى للـ prompt،
    عشان الـ AI يبني تقييمه على شواهد حقيقية مش انطباع → دقة أعلى.
    """
    if not signals:
        return ""

    def _hits(items):
        words = [h.get("word", "") for h in (items or [])][:8]
        return "، ".join(w for w in words if w) or "لا يوجد"

    return (
        "\nإشارات مُستخرجة آلياً من الصوت (استخدمها كأدلة موضوعية في تقييمك):\n"
        f"- وتيرة كلام المندوب: {signals.get('rep_speaking_pace_wpm', 0)} كلمة/دقيقة\n"
        f"- وتيرة كلام العميل: {signals.get('cust_speaking_pace_wpm', 0)} كلمة/دقيقة\n"
        f"- أطول صمت: {signals.get('longest_silence_sec', 0)} ثانية\n"
        f"- أطول حديث متواصل للمندوب (monologue): {signals.get('longest_monologue_sec', 0)} ثانية\n"
        f"- عدد المقاطعات: {signals.get('interruption_count', 0)}\n"
        f"- كلمات خطر ظهرت: {_hits(signals.get('danger_hits'))}\n"
        f"- كلمات فرصة ظهرت: {_hits(signals.get('opportunity_hits'))}\n"
        f"- إشارات تردد ظهرت: {_hits(signals.get('hesitation_hits'))}\n"
        "ملاحظة: وتيرة كلام عالية جداً أو monologue طويل = استماع أضعف. "
        "كلمات الفرصة = اهتمام أعلى. كلمات الخطر/التردد = مخاطرة أعلى على الإغلاق.\n"
    )


def _calc_total_score(scores: dict) -> tuple[float, str]:
    weights = {"listening_score": 0.25, "discovery_score": 0.20,
               "objection_score": 0.25, "next_steps_score": 0.15, "closing_score": 0.15}
    max_per = {"listening_score": 25, "discovery_score": 20,
               "objection_score": 25, "next_steps_score": 15, "closing_score": 15}
    total = sum((scores.get(k, 0) / max_per[k]) * 100 * w for k, w in weights.items())
    total = round(min(max(total, 0), 100), 1)
    if   total >= 90: grade = "A+"
    elif total >= 80: grade = "A"
    elif total >= 70: grade = "B+"
    elif total >= 60: grade = "B"
    elif total >= 50: grade = "C"
    elif total >= 40: grade = "D"
    else:             grade = "F"
    return total, grade


def analyze_transcript(
    transcript:        str,
    customer_name:     str,
    duration_seconds:  int,
    talk_ratio:        float,
    customer_industry: str = "",   # ← جديد
    signals:           dict = None,  # ← إشارات محسوبة آلياً (أدلة موضوعية)
) -> dict:
    duration_minutes = max(1, duration_seconds // 60)

    # Industry context
    industry_context = ""
    if customer_industry and customer_industry in INDUSTRY_CONTEXTS:
        industry_context = f"سياق القطاع:\n{INDUSTRY_CONTEXTS[customer_industry].strip()}\n"

    prompt = ANALYSIS_PROMPT.format(
        company_name     = settings.COMPANY_NAME,
        product_name     = settings.PRODUCT_NAME,
        target_market    = settings.TARGET_MARKET,
        sales_cycle      = settings.SALES_CYCLE_DAYS,
        customer_name    = customer_name,
        duration_minutes = duration_minutes,
        talk_ratio       = talk_ratio,
        transcript       = _window_transcript(transcript),
        scoring_criteria = SCORING_CRITERIA,
        industry_context = industry_context,
        signals_block    = _build_signals_block(signals),
    )

    start_time = time.time()
    model_used = "groq"
    raw_text   = None

    # ✅ PERF/COST FIX: كاش نتيجة التحليل بـ hash للـ prompt كامل —
    # إعادة المعالجة (retry بعد فشل خطوة لاحقة، أو إعادة تشغيل يدوية)
    # لنفس الـ transcript ما بتدفعش لـ Groq تاني. temperature=0.1 شبه
    # deterministic فالنتيجة المكاشة مكافئة. TTL أسبوع.
    import hashlib
    _cache_key = "ai_analysis:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    try:
        from ..utils.redis_client import redis_client as _redis
        _cached = _redis.get(_cache_key)
        if _cached:
            cached_result = json.loads(_cached)
            cached_result["analysis"]["ai_model_used"] = (
                cached_result["analysis"].get("ai_model_used", "") + " (cached)"
            )
            print("💾 AI analysis served from cache — no provider call")
            return cached_result
    except Exception:
        pass  # الكاش تحسين — لو Redis واقع نكمل عادي

    try:
        raw_text = _call_groq(prompt)
    except Exception as e:
        print(f"⚠️ Groq failed: {e} — trying Gemini")
        try:
            raw_text   = _call_gemini(prompt)
            model_used = "gemini"
        except Exception as e2:
            raise RuntimeError(f"Both AI providers failed: {e2}")

    processing_time = int(time.time() - start_time)

    # ── Parse مع retry: لو فشل الـ parse نجرّب المزوّد الآخر مرة واحدة ──
    try:
        data = _parse_json_response(raw_text)
    except Exception as e:
        print(f"⚠️ JSON parse failed ({e}) — retrying with fallback provider")
        try:
            if model_used == "groq":
                raw_text   = _call_gemini(prompt)
                model_used = "gemini"
            else:
                raw_text   = _call_groq(prompt)
                model_used = "groq"
            data = _parse_json_response(raw_text)
        except Exception as e2:
            raise RuntimeError(f"Failed to parse AI response: {e2}\nRaw: {raw_text[:500]}")

    scores_raw  = _clamp_scores(data.get("scores", {}))
    total_score, grade = _calc_total_score(scores_raw)

    # ── Decision maker ──────────────────────────────
    dm = data.get("decision_maker", {})
    decision_maker = {
        "level":          dm.get("level", "unknown"),
        "confidence":     dm.get("confidence", "low"),
        "signals":        dm.get("signals", []),
        "recommendation": dm.get("recommendation", ""),
    }

    # ── Sentiment trajectory ─────────────────────────
    st = data.get("sentiment_trajectory", {})
    sentiment_trajectory = {
        "opening":       st.get("opening", "neutral"),
        "middle":        st.get("middle",  "neutral"),
        "closing":       st.get("closing", "neutral"),
        "trend":         st.get("trend",   "stable"),
        "turning_point": st.get("turning_point"),
    }

    # ── Competitor intel ─────────────────────────────
    ci = data.get("competitor_intel", {})
    competitor_intel = {
        "mentioned":            ci.get("mentioned", False),
        "names":                ci.get("names", []),
        "customer_reaction":    ci.get("customer_reaction"),
        "competitive_response": ci.get("competitive_response"),
    }

    # ── Opening script ────────────────────────────────
    os_data = data.get("opening_script", {})
    opening_script = {
        "line1": os_data.get("line1", ""),
        "line2": os_data.get("line2", ""),
        "line3": os_data.get("line3", ""),
        "tip":   os_data.get("tip",   ""),
    }

    result = {
        "analysis": {
            "summary":              data.get("summary", ""),
            "sentiment_trajectory": sentiment_trajectory,
            "customer_questions":   data.get("customer_questions", []),
            "customer_pain_points": data.get("customer_pain_points", []),
            "customer_interest":    data.get("customer_interest", "medium"),
            "decision_maker":       decision_maker,
            "competitor_intel":     competitor_intel,
            "objections":           data.get("objections", []),
            "rep_strengths":        data.get("rep_strengths", []),
            "rep_weaknesses":       data.get("rep_weaknesses", []),
            "missing_topics":       data.get("missing_topics", []),
            "coaching_notes":       data.get("coaching_notes", ""),
            "opening_script":       opening_script,
            "next_steps":           data.get("next_steps", []),
            "follow_up_days":       data.get("follow_up_days", 2),
            "closing_probability":  data.get("closing_probability", 50),
            "deal_stage":           data.get("deal_stage", "qualified"),
            "talk_ratio":           talk_ratio,
            "ai_model_used":        model_used,
            "processing_time_sec":  processing_time,
        },
        "scores": {
            "listening_score":  scores_raw.get("listening_score", 0),
            "discovery_score":  scores_raw.get("discovery_score", 0),
            "objection_score":  scores_raw.get("objection_score", 0),
            "next_steps_score": scores_raw.get("next_steps_score", 0),
            "closing_score":    scores_raw.get("closing_score", 0),
            "total_score":      total_score,
            "grade":            grade,
        },
    }

    try:
        _redis.setex(_cache_key, 7 * 24 * 3600, json.dumps(result, ensure_ascii=False))
    except Exception:
        pass

    return result
