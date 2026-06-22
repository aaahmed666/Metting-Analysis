# Meetings Integration — Addendum (Upload + 4 result endpoints)

This extends the auth bundle. It wires the **real file upload** and adds the
backend endpoints + DB table behind the **meetings, transcript, analysis, and
scoring** screens. No AI yet — the rich payloads are stored as JSONB and stay
empty (UI shows a "processing/not ready" state) until a pipeline fills them.

## Backend

### New files
```
migrations/002_meetings.sql        # Meetings table (+ JSONB payload columns)
app/core/meetings_service.py       # DB access + serializers to FE shapes
app/api/meetings.py                # 7 endpoints, mounted at /api/v1/meetings
main.py                            # REPLACES — registers meetings_router
```

### Run the migration
Run `migrations/001_auth_cycle.sql` first (it creates `Organizations`, which
`Meetings.org_id` references), then `migrations/002_meetings.sql`.

### Endpoints (all under `/api/v1/meetings`, all require a Bearer token)
| Method | Path | Returns |
|---|---|---|
| POST | `/uploads` | `{data:{meetingId,status}}` — registers a stored upload |
| GET  | `` (list) | `{data: Meeting[]}` |
| POST | `/{id}/retry` | `{data: Meeting}` |
| GET  | `/analyses?search&status&sentiment&sortBy&sortDir&page&pageSize` | `{data: Paginated<MeetingAnalysis>}` |
| GET  | `/{id}/analysis` | `{data: MeetingDetail}` |
| GET  | `/{id}/deep-dive` | `{data: MeetingDeepDive}` or 409 if not ready |
| GET  | `/{id}/scoring` | `{data: SalesScore}` or 409 if not ready |

Responses use the `{ "data": ... }` envelope (the meeting/scoring frontend
services unwrap `data.data`). Rows are scoped to the caller's `org_id`
(resolved from `public.Users`).

### Status lifecycle
`uploaded → processing → transcribing → analyzing → scoring → completed`
(`failed` is retryable via `/{id}/retry`). On create the row is `uploaded`
with empty payloads. Deep-dive and scoring return **409** until their JSONB is
populated — the frontend can treat 409 as "still processing".

## Frontend

### Changed files
```
packages/api/src/meeting.service.ts                          # + real uploadFile, fileId passthrough
apps/web/src/features/meetings/queries/use-create-upload.ts  # accepts fileId/fileUrl
apps/web/src/features/meetings/hooks/use-file-upload.ts      # REAL upload w/ live progress
```

### What changed in behavior
Before: the upload was *simulated* (fake progress, file never sent). Now:
1. `meetingService.uploadFile(file, onProgress)` → multipart `POST /upload`
   (mounted at the API root, not under `/api/v1`) → returns `{fileId, url, …}`
   with **live** `onUploadProgress`.
2. `createUpload({…, fileId, fileUrl})` → `POST /meetings/uploads` records the
   meeting row.

`scoring.service.ts` already pointed at `/meetings/{id}/scoring` — it now hits
the real endpoint with `NEXT_PUBLIC_ENABLE_MOCKS=false`. No change needed there.

### CORS note
`POST /upload` is at the root path. Its CORS is governed by the same
`CORS_ORIGINS` setting (already includes localhost:3000). If you serve the
frontend from another origin, add it there.

## Verifying without AI
Until the pipeline runs, you can hand-populate a row to see the analysis/scoring
screens render real data. Example (Supabase SQL editor) — set a completed
meeting with minimal payloads:

```sql
update public."Meetings"
set status='completed', score=82, sentiment='positive', company='Acme',
    duration_minutes=32, insights='["High Intent","Pricing discussed"]'::jsonb,
    detail_data = jsonb_build_object(
      'summary','Strong discovery call.', 'summaryTags', '["discovery"]'::jsonb,
      'participants','[]'::jsonb, 'transcript','[]'::jsonb,
      'highlights','[]'::jsonb, 'nextSteps','[]'::jsonb,
      'sentimentTimeline','[]'::jsonb, 'competitors','[]'::jsonb,
      'recordingAvailable', false, 'propensityLabel','High'
    )
where id = '<MEETING_ID>';
```

Deep-dive/scoring screens need `deepdive_data` / `scoring_data` populated with
the `MeetingDeepDive` / `SalesScore` shapes (see `packages/types/src/meeting.ts`).

## Still mock-only (no backend yet)
pipeline, deals, risks, dashboard, workflows, integrations, developer,
companies, contacts, users, billing, audit-logs, ai-settings, shell. These
remain on mocks until you build their backends (same pattern: table + router +
serializers).
