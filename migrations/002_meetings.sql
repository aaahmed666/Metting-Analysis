-- ============================================================================
-- Migration: Meetings + analysis results
-- Stores the upload-centric meeting record plus the rich analysis/scoring/
-- transcript payloads as JSONB (populated later by the AI pipeline).
-- Safe to run multiple times (IF NOT EXISTS guards).
-- ============================================================================

CREATE TABLE IF NOT EXISTS public."Meetings" (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid REFERENCES public."Organizations"(id) ON DELETE CASCADE,
    owner_id      uuid,                       -- the sales rep (auth user id)

    -- Upload-centric fields
    title         text NOT NULL DEFAULT 'Untitled meeting',
    file_name     text,
    file_id       text,                       -- id returned by /upload
    file_url      text,
    size_bytes    bigint NOT NULL DEFAULT 0,
    mime_type     text,

    -- Processing lifecycle:
    -- uploaded | processing | transcribing | analyzing | scoring | completed | failed
    status        text NOT NULL DEFAULT 'uploaded',
    progress      int  NOT NULL DEFAULT 0,     -- 0..100 for the active stage
    duration_minutes        numeric,
    estimated_minutes_left  numeric,

    -- Analytical columns (filled once analysis completes)
    company       text,
    score         int,                         -- 0..100 deal score
    sentiment     text,                         -- very_positive|positive|neutral|critical
    deal_value    numeric,
    insights      jsonb NOT NULL DEFAULT '[]'::jsonb,   -- string[] short tags

    -- Rich payloads (shape mirrors the frontend types; null until ready)
    detail_data   jsonb,   -- MeetingDetail (participants, transcript, highlights…)
    deepdive_data jsonb,   -- MeetingDeepDive
    scoring_data  jsonb,   -- SalesScore

    error_message text,
    uploaded_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_meetings_org    ON public."Meetings"(org_id);
CREATE INDEX IF NOT EXISTS idx_meetings_owner  ON public."Meetings"(owner_id);
CREATE INDEX IF NOT EXISTS idx_meetings_status ON public."Meetings"(status);

-- Keep updated_at fresh on writes.
CREATE OR REPLACE FUNCTION public.touch_meetings_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_meetings_updated_at ON public."Meetings";
CREATE TRIGGER trg_meetings_updated_at
    BEFORE UPDATE ON public."Meetings"
    FOR EACH ROW EXECUTE FUNCTION public.touch_meetings_updated_at();
