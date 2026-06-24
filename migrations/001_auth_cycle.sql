-- ============================================================================
-- Migration: Full Auth Cycle support
-- Adds: Organizations bootstrap, Invites, Two-Factor email challenges.
-- Safe to run multiple times (IF NOT EXISTS guards).
-- ============================================================================

-- --- Organizations -----------------------------------------------------------
-- Self-signup creates a row here; invite flow references an existing one.
CREATE TABLE IF NOT EXISTS public."Organizations" (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- --- Users: make sure 2FA preference column exists ---------------------------
ALTER TABLE public."Users"
    ADD COLUMN IF NOT EXISTS two_factor_enabled boolean NOT NULL DEFAULT false;

-- --- Invites -----------------------------------------------------------------
-- A manager/admin invites a user to an org (+ optional team) with a role.
-- The invitee registers using the token; org/role/team are taken from here.
CREATE TABLE IF NOT EXISTS public."Invites" (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token       text NOT NULL UNIQUE,
    email       text NOT NULL,
    org_id      uuid NOT NULL REFERENCES public."Organizations"(id) ON DELETE CASCADE,
    team_id     uuid,
    role        text NOT NULL DEFAULT 'sales_rep',
    invited_by  uuid,
    accepted    boolean NOT NULL DEFAULT false,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_invites_token ON public."Invites"(token);
CREATE INDEX IF NOT EXISTS idx_invites_email ON public."Invites"(email);

-- --- Two-Factor email challenges --------------------------------------------
-- Short-lived 6-digit codes emailed to the user during login.
CREATE TABLE IF NOT EXISTS public."TwoFactorChallenges" (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL,
    email         text NOT NULL,
    code_hash     text NOT NULL,          -- sha256 of the 6-digit code
    attempts      int  NOT NULL DEFAULT 0,
    consumed      boolean NOT NULL DEFAULT false,
    expires_at    timestamptz NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_2fa_user ON public."TwoFactorChallenges"(user_id);
