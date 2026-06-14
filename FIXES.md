# FIXES.md — Architecture Review Fixes (June 2026)

All issues from the deep code review have been resolved. Summary of changes:

## 1. Chunk Processing Order (`services/chunking_service.py`)
- **Verified safe:** final transcript order was already deterministic (index-slotted
  results + sorted merge). No change needed for ordering itself.
- **Fixed — context race:** `previous_texts` shared-list race removed. Default path is
  now sequential with a fully correct context chain (no speed loss — GPU inference is
  serialized anyway). Parallel path (multi-GPU only) passes context **by value** at
  submit time; no shared mutable state.
- **Fixed — overlap text duplication:** merged transcript is now rebuilt from the
  de-overlapped, time-filtered segments instead of raw chunk texts. No more ~10s of
  duplicated speech at every chunk boundary feeding the AI analysis.

## 2. Zoom Webhook Memory (`api/zoom_webhook.py`)
- **Fixed:** download now streams to disk in 1MB chunks (`httpx.stream`) — RAM is O(1MB)
  per recording instead of O(file size). Concurrent recordings are safe.
- **Fixed:** background handler is now sync `def` → runs in FastAPI's threadpool;
  blocking DB queries and file writes no longer freeze the event loop.
- **Added:** idempotency via Redis (`zoom_recording:{uuid}`, SET NX) — Zoom webhook
  retries no longer create duplicate meetings or double-process recordings.
- Same event-loop fix applied to `api/rgeeb_callback.py`.

## 3. Speaker Diarization (`services/diarization_service.py` — new)
- **Added:** optional real diarization via `pyannote/speaker-diarization-3.1`
  (embeddings + clustering), run on the **full audio** once (consistent clusters),
  mapped onto Whisper segments by maximal temporal overlap, with real per-segment
  confidence. Role mapping: first-speaking cluster = sales_rep.
- Enable with `DIARIZATION_ENABLED=true` + `HF_TOKEN=hf_...` +
  `pip install pyannote.audio`. If anything is missing, the pipeline transparently
  falls back to the existing heuristics — zero downtime.
- `workers/tasks.py` Step 2.5 recomputes `talk_ratio` from the corrected labels
  (this feeds the 25-point listening score).
- Recommended next step: persistent rep voice profiles (ECAPA-TDNN embeddings) for
  role mapping that doesn't rely on "first speaker = rep".

## 4. AI Transcript Window (`services/ai_service.py`, `config.py`)
- The reviewed value was **14000 chars** (not 12000; nothing in the codebase was 12000).
- **Fixed:** limit raised to `AI_MAX_TRANSCRIPT_CHARS = 48000` (config-driven,
  ≈ 20–30K tokens of Arabic — well within llama-3.3-70b's 128K context). Lower it
  via `.env` only if you hit Groq TPM rate limits.
- **Fixed:** overflow strategy is now head (45%) + middle sample (15%) + tail (40%)
  instead of head+tail — discovery/objections in the middle are no longer fully
  discarded for very long meetings.

## 5. Model Loading (`services/whisper_service.py`)
- **Verified:** Whisper was already a process-level singleton (loaded once per worker,
  reused across tasks; solo pool). No per-task reload existed.
- **Fixed — double-load race:** `get_model()` now uses double-checked locking.
- **Fixed — thread-unsafe inference:** `model.transcribe()` is serialized with an
  inference lock (openai-whisper installs KV-cache hooks on the shared model and is
  not safe under concurrent transcription). `CHUNK_WORKERS` default lowered to 1.
- **Fixed — API memory bloat:** new `workers/queue.py` dispatches `process_meeting`
  by name; `api/meetings.py` and `api/zoom_webhook.py` no longer import
  `workers.tasks` → torch/whisper are never imported into the FastAPI process.

## Additional fixes
- **Retry status flow** (`workers/tasks.py`): meetings now show "processing" with a
  retry note during backoff; "failed" is set only after retries are exhausted (was:
  marked failed before retrying, silently swallowed final failure).
- **Follow-up reminders**: SQL-windowed query (63-day window) + single JOIN for users
  — replaces unbounded full-table load and per-meeting N+1 lookups.
- **Temp cleanup race**: `cleanup_temp_files` now skips files belonging to meetings in
  active pipeline statuses and uses a 6h age window (was 1h — could delete an R2
  download for a queued job).
- **Duplicate scheduler removed**: dead `utils/scheduler.py` (schedule-library thread
  duplicating Celery beat's daily report) deleted; unused `schedule` dependency removed.

---

# Round 2 — Security & Performance Audit Fixes (June 2026)

## 🔴 Critical security fixes

### 1. Privilege escalation via public registration (`api/auth.py`)
- **Fixed:** `POST /api/auth/register` accepted `role` (and `team_id`) from the
  request body and wrote it verbatim — `curl ... {"role": "admin"}` produced an
  unauthenticated admin account. `role` and `team_id` are removed from the
  schema entirely; the endpoint hard-codes `role="sales"`, `team_id=None`.
  Role assignment remains admin-only via `POST /api/admin/users`.
- Frontend `authAPI.register` no longer sends `role`.

### 2. Webhook SSRF (`services/webhook_service.py`, `api/admin.py`, new `utils/url_safety.py`)
- **Fixed:** outbound webhook URLs are validated **twice** — at registration
  (`add_webhook` rejects with 400) and again before every dispatch (DNS can
  change after registration — rebinding). Rules: https-only; hostname must
  resolve exclusively to public IPs (private/loopback/link-local/reserved/
  multicast rejected, IPv4+IPv6); `follow_redirects=False` so a 302 can't
  bounce into the VPC. Single shared httpx client per dispatch run.

### 3. Zoom fail-open signature + download SSRF (`api/zoom_webhook.py`)
- **Fixed:** empty `ZOOM_WEBHOOK_SECRET` now **rejects** all webhooks when
  `ENVIRONMENT=production` (dev keeps the permissive behavior).
- **Added:** `download_url` must be https on an official Zoom domain
  (`*.zoom.us` / `*.zoomgov.com`) resolving to public IPs.
- **Added:** size guard — declared `file_size` over `MAX_FILE_SIZE_MB` is
  skipped before download, and the limit is enforced again during streaming
  (Content-Length can lie).

### 4. SECRET_KEY strength (`config.py`)
- **Fixed:** app refuses to start in production with a key < 32 chars
  (fail closed); development gets a warning only.

### 5. CSRF Origin check (`main.py`)
- **Added:** middleware rejects state-changing requests (POST/PUT/PATCH/DELETE)
  whose `Origin` header is present but not in the allowed origins. Complements
  `SameSite=lax`. Server-to-server webhooks (no Origin header) are unaffected.

## 🟠 Performance & architecture fixes

### 6. N+1 admin analytics (`api/admin.py`)
- `get_stats`: 3 aggregate queries instead of loading every meeting +
  lazy-loading score/analysis per row.
- `get_leaderboard`: single JOIN + GROUP BY (was O(reps) meeting queries +
  O(reps×meetings) lazy loads ≈ 10,000+ queries per cache miss at scale).
- `get_activity`: one bulk meetings query with `selectinload(score)` for all
  reps, grouped in Python (needs per-meeting timestamps for daily/hourly stats).

### 7. Redis `keys("admin:*")` scan removed (`utils/redis_client.py`, `api/meetings.py`)
- Cache keys are tracked in a Redis Set (`admin:cache_index`);
  `invalidate_admin_cache()` deletes tracked members only — no O(N) keyspace
  scan blocking Redis. Invalidation also added to user create/update/deactivate.

### 8. SSE via Redis pub/sub (`api/meetings.py`, `workers/tasks.py`)
- `_update_status` publishes every status change to `meeting_status:{id}`.
- The SSE endpoint subscribes instead of opening a DB session every 5s for up
  to 30 min per browser tab (was exhausting the 10+20 connection pool).
  Fallback DB check every 30s covers missed messages; 30-min hard cap kept.

### 9. Notifications off the critical path (`workers/tasks.py`)
- Webhook dispatch and the "analysis ready" email are now separate Celery
  tasks (`.delay`) with their own retries — they no longer block the pipeline
  or entangle with its retry logic.

### 10. Bulk segment inserts (`workers/tasks.py`)
- `bulk_save_objects` for SpeakerSegments (hundreds of rows on long meetings).

### 11. faster-whisper backend (`services/whisper_service.py`, `config.py`)
- Opt-in via `WHISPER_BACKEND=faster` (+ `pip install faster-whisper`):
  CTranslate2, 3–5× faster, ~40–60% less VRAM, same accuracy. Output is
  normalized to the existing segment-dict shape, so chunking/diarization/
  signals run unchanged. Auto-fallback to openai-whisper if not installed.
  `WHISPER_COMPUTE_TYPE` configurable (auto: int8_float16 cuda / int8 cpu).

### 12. Skip redundant re-encode (`services/audio_service.py`)
- If the source is already WAV PCM 16 kHz mono, the FFmpeg pass is skipped.

### 13. AI result cache (`services/ai_service.py`)
- Analysis results cached in Redis by SHA-256 of the full prompt (TTL 7 days).
  Pipeline retries / manual re-runs of the same transcript no longer re-bill
  Groq. Cache failures degrade silently.

### 14. Audio URL construction (`api/meetings.py`, `config.py`)
- `FRONTEND_URL.replace(":3000", ":8000")` hack replaced by `API_PUBLIC_URL`
  setting, falling back to the request's own base URL (proxy-aware).

### 15. Typed deal-stage body (`api/meetings.py`)
- Raw `dict` replaced by `StageUpdate` with `Literal[...]` — invalid values
  rejected by Pydantic, schema visible in OpenAPI.

## 🔭 Observability

### 16. Structured logging + optional Sentry (new `utils/logging_config.py`)
- Unified `timestamp | level | module | message` logging configured for both
  the API (main.py) and the Celery worker (tasks.py). `LOG_LEVEL` env-driven.
- Set `SENTRY_DSN` (+ `pip install sentry-sdk`) to enable error tracking.
- New code logs via `logging`; legacy `print()`s remain in untouched modules —
  migrate opportunistically.

## 🖥️ Frontend fixes

### 17. Route protection (`middleware.ts` — new)
- Server-side middleware: unauthenticated users are redirected to /login
  before protected pages render (no more flash); expired tokens treated as
  logged-out; `sales` role redirected away from `/admin/*`; authed users
  redirected away from /login and /register; `/` routes by role.
  (Role decode is unverified-payload UX gating — the backend remains the
  actual authorization boundary.)

### 18. Audio leak / overlapping playback (`app/meetings/[id]/page.tsx`)
- Single `useRef<HTMLAudioElement>`: previous audio paused before any new
  play, cleanup on unmount, and clicking the active timestamp now toggles
  pause. No more stacked simultaneous streams or leaked Audio elements.

### 19. Debounce off the window global (`app/dashboard/page.tsx`)
- `(window as any)._searchTimeout` replaced with a local ref + unmount
  cleanup (no setState-after-unmount, no cross-instance clobbering).

### 20. SSE callbacks via ref (`hooks/useMeetingStatus.ts`)
- `onAnalyzed`/`onFailed` read from a ref updated each render — latest
  closures always fire; the `eslint-disable` is gone.

## Deliberately deferred (documented, not done)
- **Tailwind migration / splitting the 600–700-line pages** — a wholesale
  restyle of working UI is high-regression-risk to do blind; recommend doing
  it page-by-page with visual review.
- **pyannote→ECAPA persistent voice profiles, RAG/hierarchical summarization,
  calibrated closing probability, Instructor structured outputs** — new
  feature work, not fixes; designs are in the audit (Parts 5–7).
- **Multi-GPU task-per-chunk chord** — only relevant when hardware changes.

## New configuration reference (.env)
```
API_PUBLIC_URL=          # e.g. https://api.example.com (audio links; empty = derive from request)
WHISPER_BACKEND=openai   # set "faster" after pip install faster-whisper
WHISPER_COMPUTE_TYPE=    # empty = auto (int8_float16 cuda / int8 cpu)
SENTRY_DSN=              # optional, requires sentry-sdk
LOG_LEVEL=INFO
# production hard requirements now enforced:
#   SECRET_KEY  >= 32 chars (app refuses to start otherwise)
#   ZOOM_WEBHOOK_SECRET must be set or all Zoom webhooks are rejected
```
