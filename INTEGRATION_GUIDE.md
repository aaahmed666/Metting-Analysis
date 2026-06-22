# Auth Cycle Integration — Backend ⇄ Frontend

This bundle wires the Next.js frontend to the FastAPI + Supabase backend for the
**full authentication cycle**, adds the auth pieces that were missing from the
backend, and adds Google sign-in.

The golden rule used throughout: **the frontend adapts to the backend's response
shape**. A single adapter in `auth.service.ts` translates the backend's
snake_case / nested-token responses into the frontend's domain model. Nothing
else in the frontend had to learn the backend's wire format.

---

## 1. Decisions baked in

| Topic | Decision |
|---|---|
| Response shape | Backend's shape is canonical; frontend adapts via `auth.service.ts`. |
| `role` values | Unified on backend values: `sales_rep`, `manager`, `admin`. Frontend `'representative'` removed. |
| Registration flows | **self-signup** (creates a new org, user becomes its `admin`) **and invite** (org/role/team come from the invite). |
| Google OAuth | New user defaults to `sales_rep`. Backend-driven (start URL + callback provisioning). |
| 2FA | **Email 6-digit code** (matches the existing frontend). Sent via SMTP if configured, else logged; `000000` accepted outside production. |
| Missing-in-backend items | Built in the backend: `verify-email`, `reset-password`, 2FA, Google, invites. |
| Token refresh | Added silent auto-refresh in the Axios interceptor. |
| Route protection | Added `AuthGuard` on the dashboard layout. |

---

## 2. What changed — Backend

Run the migration, drop in the new/updated files, and merge the settings.

### 2.1 Database migration
`migrations/001_auth_cycle.sql` — run it once against your Supabase Postgres
(SQL editor or `psql`). Creates:
- `Organizations` (self-signup target)
- `Invites` (token, email, org_id, role, team_id, expiry)
- `TwoFactorChallenges` (hashed 6-digit codes, attempts, expiry)
- adds `Users.two_factor_enabled`

### 2.2 New / replaced files
```
app/api/auth.py                        # REPLACES existing — all endpoints
app/models/auth_models.py              # REPLACES existing — new request models
app/core/auth_services.py              # NEW — orgs / invites / 2FA helpers
services/integrations/email_sender.py  # NEW — SMTP w/ dev-log fallback
migrations/001_auth_cycle.sql          # NEW
```

### 2.3 Settings to merge
Open `config/setting.py` and paste the block from
`config/setting_additions.py` into the `Settings` class (2FA, SMTP, frontend
URL, invite TTL). All have safe defaults.

### 2.4 Endpoints now available (`/api/v1/auth/...`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/register` | self-signup / invite / direct |
| POST | `/login` | returns session OR a 2FA challenge |
| POST | `/two-factor` | verify email code → session |
| POST | `/two-factor/resend` | resend code |
| POST | `/logout` | |
| POST | `/refresh` | refresh tokens |
| GET  | `/me` | current profile |
| POST | `/forgot-password` | send reset email |
| POST | `/reset-password` | reset using recovery token |
| PATCH| `/update-password` | change password while signed in |
| POST | `/verify-email` | confirm email (token_hash) |
| POST | `/resend-verification` | resend confirmation |
| POST | `/google` | get Google authorization URL |
| POST | `/google/callback` | finish Google sign-in + provision user |
| POST | `/invites` | (manager/admin) create + email an invite |
| GET  | `/invites/{token}` | preview an invite (prefill register) |

### 2.5 Supabase config to set in the dashboard
- **Auth → Providers → Google**: enable, add client ID/secret.
- **Auth → URL Configuration → Redirect URLs**: add
  `http://localhost:3000/login` and `http://localhost:3000/reset-password`
  (and your prod equivalents).
- 2FA email codes are sent by *our* SMTP (or logged); they do **not** use
  Supabase email. Verify-email and reset-password **do** use Supabase email.

> **Note on the 2FA session mint:** after the code is verified, the backend
> mints a real session without the password using the supported
> `admin.generate_link(type="magiclink")` → `verify_otp(token_hash, "magiclink")`
> pattern. This is the only robust cross-version path in supabase-py.

---

## 3. What changed — Frontend

### 3.1 The adapter (the heart of it)
`packages/api/src/auth.service.ts` — rewritten. `httpAuthApi` calls the real
endpoints and maps:
- `user.access_token` / `refresh_token` (nested, snake_case) →
  `session.tokens.{accessToken, refreshToken}` (nested object, camelCase)
- `two_factor_required` / `challenge_id` / `masked_email` → the discriminated
  `LoginResult`
- request bodies are mapped back to snake_case (`full_name`, `invite_token`,
  `organization_name`, `new_password`, …)

### 3.2 Files changed
```
packages/types/src/auth.ts                 # role unified, new inputs/results
packages/api/src/auth.service.ts           # adapter + new methods
packages/api/src/schemas/auth.schema.ts    # register org/invite, reset uses accessToken
packages/api/src/client/axios.ts           # auto-refresh on 401
packages/api/src/client/query-keys.ts      # + google/invite mutation keys (see PATCH note)
packages/api/src/mock/{db,auth.mock,admin-settings.data}.ts  # role → sales_rep, new input fields
packages/config/src/env.ts                 # default API → :8000/api/v1
packages/config/src/navigation.ts          # ROLE_* keys → sales_rep

apps/web/src/lib/session-store.ts          # refresh-token access + updateTokens
apps/web/src/providers/providers.tsx       # wire refresh callbacks
apps/web/src/app/[locale]/(dashboard)/layout.tsx  # AuthGuard
apps/web/src/app/[locale]/(auth)/reset-password/page.tsx
apps/web/src/app/[locale]/(auth)/verify-email/page.tsx

apps/web/src/features/auth/queries/*       # use-register/-reset/-verify updated; +use-me/-google/-invite
apps/web/src/features/auth/components/*     # login/register/reset/verify/forgot forms;
                                            # +google-button (functional), +google-callback-handler,
                                            # +reset-password-client, +auth-guard
apps/web/src/features/shell/hooks/use-current-user.ts  # demo role → sales_rep, +nullable accessor
apps/web/src/features/users/components/users-screen.tsx # role enum/defaults → sales_rep
```

### 3.3 Two tiny manual merges
- **`query-keys.ts`** — add the three keys shown in
  `client/query-keys.PATCH.ts` (googleStart / googleCallback / createInvite).
  (Already applied if you copy the provided `query-keys.ts`.)
- **`constants.ts`** — see `constants.PASSWORD_NOTE.ts`. Frontend currently
  enforces 12-char passwords (stricter than the backend's 8). Keeping 12 is
  safe. Lower to 8 only if product wants it.

### 3.4 i18n keys to add
The register form now shows organization + invite copy. Add these message keys
(en + ar) under `auth.register`:
```
organizationLabel, organizationPlaceholder, invitedAs   // "Invited as {role}"
```
And ensure `validation.organizationName.required` exists. (If a key is missing,
next-intl renders the key string — harmless but ugly.)

---

## 4. End-to-end flows

**Self-signup:** Register page → fill name/email/password/**organization name**
→ `POST /register` creates org + admin user → redirect to login.

**Invite:** Manager calls `useCreateInvite({email, role, teamId})` → backend
emails a link `…/register?invite=<token>` → invitee opens it → form prefills
email (locked) + shows role → `POST /register` with `invite_token`.

**Login (no 2FA):** `POST /login` → session persisted → dashboard.

**Login (2FA on):** `POST /login` → `two_factor_required` → OTP screen →
`POST /two-factor` → session → dashboard. Resend via `/two-factor/resend`.

**Google:** click Google → `POST /google` → redirect to Google → back to
`/login#access_token=…` → `GoogleCallbackHandler` posts to `/google/callback`
(provisions user on first login, default `sales_rep`) → session → dashboard.

**Forgot/reset:** Forgot page sends `redirectTo=…/reset-password` → Supabase
emails a recovery link → lands on `/reset-password#access_token=…` →
`ResetPasswordClient` extracts the token → `POST /reset-password`.

**Verify email:** Supabase confirmation link → `/verify-email?token_hash=…` →
auto-verifies via `POST /verify-email`.

**Silent refresh:** any 401 triggers one `POST /refresh`; on success the request
is retried with the new token and the session is updated; on failure the session
is cleared and the user is sent to login.

---

## 5. Run it

```bash
# Backend
cd backend
pip install -r requirements.txt
cp .env.example .env            # fill Supabase + S3 creds
# run migrations/001_auth_cycle.sql in Supabase
uvicorn main:app --reload --port 8000

# Frontend
cd frontend
pnpm install
cp apps/web/.env.example apps/web/.env.local   # NEXT_PUBLIC_ENABLE_MOCKS=false
pnpm --filter web dev           # http://localhost:3000
```

Toggle `NEXT_PUBLIC_ENABLE_MOCKS=true` to fall back to the in-memory mock
(useful for UI work without the backend).

---

## 6. Verification done in this bundle
- All backend `.py` files compile (`py_compile`).
- All changed frontend `.ts/.tsx` files transpile cleanly (TypeScript
  transpile pass). A full `tsc` type-check requires installing the monorepo
  deps (`pnpm install`) — run it once after copying the files in.

## 7. Known follow-ups (out of scope but flagged)
- The Users screen "create user" still uses the mock admin CRUD. To issue real
  org invites from that screen, wire its modal to `useCreateInvite`.
- Route protection is client-side (`AuthGuard`). For hard protection move the
  session to an httpOnly cookie and add a check in `middleware.ts`.
- Add the i18n keys listed in §3.4.
