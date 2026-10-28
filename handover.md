# Handover — SurPro Labs AI

Read this first. It explains what the project is, how it is built, what is done, what is not,
and what must not happen. The companion docs hold the detail:

- `CLAUDE.md` — project instructions, structure, commands and engineering rules. Authoritative
  for how to work here.
- `local_folder/plan.md` — the phased plan for Task 4 (voice rebilling). Gitignored; exists only
  on this machine.
- `local_folder/CriticalFindings.md` — every contradiction between the spec PDFs and the real
  GoHighLevel / Retell / Twilio APIs, the financial safeguards, and the open questions.
  Gitignored; exists only on this machine.
- `local_folder/pdfs/` — the four source PDFs (backend tasks, rebilling, snapshot plan, Branwynn
  scope). Gitignored.

---

## 1. What the business is

Jerry Miller runs **SurPro Labs AI**. He resells a white-label GoHighLevel (GHL) CRM to HVAC
companies for **$397/month plus a $997 setup**, and also has non-HVAC clients, e.g. **Branwynn
Destinations**, a travel retreat company. Each client is a **GHL sub-account** under Jerry's
**agency**. The GHL team builds the client-facing product inside GHL.

Example client used throughout the docs: **Cool Breeze HVAC**.

**Our scope is only what GHL cannot do:**

1. Dashboards that read across all of Jerry's client accounts.
2. **Billing for voice usage**: Retell AI voice agents and Twilio regular call minutes, split per
   client, rated per client, and billed to each client separately from their plan.

Twilio **SMS/MMS is out of scope** (GHL may rebill messaging natively).

---

## 2. The four tasks and where each stands

| # | Task | Due | Status |
|---|---|---|---|
| 1 | Marketplace app: GHL OAuth install, agency token, per-sub-account tokens | Oct 23 | **Built** (backend + onboarding page) |
| 1b | Agency admin page: client list and usage by period | — | **Built** (`/admin` live, `/admin/preview` demo; read-only) |
| 2 | Agency dashboard: MRR, live clients, leads/calls/signed by source, clients at risk, voice revenue | Oct 30 | **Not started** (`apps/dashboards` is an empty scaffold) |
| 3 | Client dashboard: one client's leads, booked jobs, response time, voice usage | Nov 4 | **Not started** (frontend feature folders hold only `.gitkeep`) |
| 4 | Voice rebilling: Retell + Twilio voice, split per client | Nov 13 | **Phases 1, 2 and 2b built**; Phase 3+ not started |

Task 2's "builds finished within 10 business days" KPI must **not** be built yet. Only plan for it:
its start and end points must be settings. Task 2's voice-revenue tile waits for Task 4.
Task 3's reviews and revenue-by-source are **on hold** (no reviews API; revenue source unconfirmed).

---

## 3. Architecture

### 3.1 Repository layout

The git repo root is `D:\tasks\jerry-miller\dashboard\`. The parent `D:\tasks\jerry-miller\` is
**not** a repo. An early session wrongly worked at `D:\tasks\jerry-miller\backend`; that path is
obsolete.

```
dashboard/
  CLAUDE.md                 project instructions: read it
  handover.md               this file
  backend/                  Django 5.2 + DRF
    config/                 settings (base / development / production / test), urls, celery.py
    apps/
      common/               EncryptedTextField (Fernet) for secrets
      ghl/                  Task 1: GHL OAuth onboarding
      rebilling/            Task 4: Retell + Twilio voice rebilling
      dashboards/           Tasks 2-3: EMPTY scaffold
    requirements/           *.in (edit) -> *.txt (pip-compile output, committed)
    .env                    secrets. Gitignored. Never commit, never print
    .env.example            every setting, documented
    ruff.toml, pytest.ini
  frontend/                 React 19 + TypeScript + Vite
    src/features/onboarding/            GHL connect page (route /onboarding): the only built feature
    src/features/agency-dashboard/      EMPTY
    src/features/client-dashboard/      EMPTY
  local_folder/             gitignored: PDFs, plan.md, CriticalFindings.md
```

### 3.2 Stack

- **Backend:** Python, Django 5.2, Django REST Framework, django-filter, django-environ,
  httpx (all outbound HTTP), cryptography (Fernet), celery[redis] with a Redis broker, whitenoise,
  gunicorn, psycopg (Postgres planned; SQLite locally by default), retell-sdk (only
  `retell.lib.verify` is used).
- **Dev and test:** pytest with pytest-django, respx (mocks httpx), factory-boy (installed, not
  used yet), pytest-cov, ruff.
- **Frontend:** React 19, TypeScript, Vite (dev server proxies `/api` to `:8000`),
  react-router-dom 7, vitest.
- **Dependency rule:** change dependencies only in `requirements/*.in`, then `pip-compile`, then
  `pip-sync`. Never `pip install` a project dependency directly.

### 3.3 Settings

`config/settings/base.py` holds everything. `development.py`, `production.py` (secure cookies,
HSTS, and `X_FRAME_OPTIONS = 'ALLOWALL'` because the app is embedded in GHL) and `test.py`
(in-memory SQLite, fast password hashing, fixed test secrets) inherit from it. `manage.py` defaults
to `config.settings.development`. pytest uses `config.settings.test` (see `pytest.ini`).

### 3.4 URL map

| Method | Path | What it does |
|---|---|---|
| GET | `/api/oauth/callback/` | GHL redirects here after install. Exchanges the code, syncs sub-accounts, redirects to the frontend |
| GET | `/api/ghl/onboarding/` | Install link, agency, sub-accounts. **Never returns tokens** |
| POST | `/api/ghl/onboarding/sync/` | Re-syncs sub-accounts from GHL |
| POST | `/api/rebilling/webhooks/retell/` | Retell `call_ended` webhook. Signature-verified, answers 204 |
| — | `/admin/` | Django admin. rebilling and ghl have **no** admin registrations yet |

---

## 4. Task 1: GHL onboarding (built)

**Flow:** the agency admin opens `/onboarding` (React) → "Connect to GoHighLevel" → GHL install
screen → GHL redirects to `GET /api/oauth/callback/?code=…` → the backend exchanges the code for
the **agency token** (`user_type=Company`) → rejects the install if it was made on a sub-account →
stores the agency row → syncs sub-accounts → redirects to
`FRONTEND_URL/onboarding?status=connected` (or `?error=install_cancelled` / `?error=install_failed`).
**Sync failures do not fail the install** — the agency connection is treated as successful once
the code is exchanged; sync logs a warning and continues.

**Model:** `GHLOAuthToken` (`apps/ghl/models.py`). One table holds both agency and sub-account
tokens. Fields: `company_id`, `location_id` (blank on the agency row), `name`, `is_agency`,
`is_active`, `access_token`, `refresh_token` (agency row only), `expires_at`. Tokens are
Fernet-encrypted with `FIELD_ENCRYPTION_KEY`. A sub-account row can have an empty `access_token`
when the app isn't yet installed on it; it will still be visible in the admin dashboard.

**Service:** `GHLOAuthService` (`apps/ghl/services.py`):

- `install_url()` builds the chooselocation URL on `GHL_INSTALL_URL_BASE`.
- `exchange_code(code)` stores the agency token. `company_id` comes **from the token response**,
  not from config.
- `get_agency_token()` refreshes the token when it is within 5 minutes of expiry. **Refresh tokens
  are single-use**, so it takes a `select_for_update` lock first.
- `get_location_token(location_id)` re-mints a sub-account token from the agency token when it is
  expiring.
- `sync_locations(agency)` calls `GET /oauth/installed-locations` with `Version: v3` header (not
  the default `2021-07-28`; v3 is required to avoid a 404), then `POST /oauth/locationToken` for
  each sub-account. It skips `AGENCY_LOCATION_ID`. If a sub-account's token can't be minted, it
  stores the row anyway with an empty `access_token` and a future `expires_at` (so it appears in
  the dashboard), logs the error with GHL's reason, and continues.
- **Error handling in `_request()`:** catches `httpx.HTTPStatusError` separately and includes the
  response body in the exception message, so GHL's error reasons are visible in logs instead of
  being discarded.

**Marketplace app config (set in GHL, must match the code):** redirect URI = `GHL_REDIRECT_URI`;
scopes = `GHL_SCOPES`; target user = Agency; auto-install on future sub-accounts.

**Default scopes** (`GHL_SCOPES`): `oauth.readonly`, `oauth.write`, `locations.readonly`,
`contacts.readonly`, `opportunities.readonly`, `calendars.readonly`, `calendars/events.readonly`,
`conversations.readonly`, `conversations/message.readonly`, `saas/company.read`,
`saas/location.read`. The `oauth.*` pair is needed to mint sub-account tokens. The spec doesn't
call it out (CriticalFindings G1).

**Frontend:** `features/onboarding/OnboardingPage.tsx`, with a vitest test. `src/api/ghl.ts` and
`src/api/client.ts` form the API layer.

**Trade-off:** removing `GHL_COMPANY_ID` means the install check only rejects sub-account
installs. It does **not** reject an install by a *different* agency. This was flagged to the user
as a security trade-off, and no replacement check was invented. The marketplace app is private,
which limits the exposure.

---

## 5. Agency admin page (Task 4 Phase 3 prerequisite, built)

The embedded admin interface Jerry opens from the GHL left nav: onboarded clients, their rates and
voice usage by period. Read-only — editing rates/mapping is Phase 4 and is not built.

**Routes:** `/admin/` is live data (requires GHL SSO); `/admin/preview` is bundled sample data
(development only, never calls the API). Both share rendering logic through `ClientBrowser.tsx`.

**How GHL identifies the viewer.** The page runs in a GHL iframe, posts
`{message: 'REQUEST_USER_DATA'}` to `window.parent`, and gets `REQUEST_USER_DATA_RESPONSE` with
an encrypted blob in `payload`. `apps/ghl/sso.py` opens it: OpenSSL-compatible AES-256-CBC
(`Salted__` + 8-byte salt, key/IV from EVP_BytesToKey with MD5) using `GHL_APP_SSO_KEY`, the
app's **Shared Secret** (Advanced Settings > Auth). Tests verify against real `openssl enc` vectors.

**Scope from `activeLocation`.** GHL reports `type: "agency"` even inside a sub-account, so
reading `type` alone would show one client every other client's numbers. The real identifier is
`activeLocation`. For the agency account itself, `activeLocation` is the agency's own `location_id`
(`AGENCY_LOCATION_ID`). `SSOContext.scope` compares it to decide `AGENCY` or `LOCATION` scope.
**Agency scope** sees all clients and their rates/usage; **location scope** sees only that
sub-account's own page. `activeLocation` is the credential that prevents cross-account viewing.

**Endpoints** (both require valid SSO blob in `X-GHL-Session`):
- `POST /api/ghl/sso/session/` — who is viewing (user name, email, role, scope, active location).
- `GET /api/rebilling/admin/clients/` — list of all sub-accounts (left-joined to `ClientMapping`),
  with `month=YYYY-MM` filter. An unmapped client shows `mapped: false` (not an error).
- `GET /api/rebilling/admin/clients/<location_id>/` — detail for one client, with usage and rates.
  Returns `{month, location_id, client_name, mapped: false}` (no usage fields) for an unmapped
  sub-account viewing its own page; agency scope requests for unmapped clients still 404 because
  the list already marked them unmapped and there is nothing to show.

**Frontend** (`features/admin/AdminPage.tsx`, `AdminPreviewPage.tsx`, `ClientBrowser.tsx`):
- Unframed pages (no GHL parent, or the SSO handshake failed) render a message linking to
  `/admin/preview` instead of showing an error.
- The preview page (`AdminPreviewPage.tsx`) renders bundled sample data (`demoData.ts`) behind an
  amber "Standalone preview" banner and **never calls the admin API**. It is for development and
  walkthrough only, not for live testing inside GHL.
- Both live and preview pages use `ClientBrowser.tsx` for list/detail rendering, so they are
  visually identical.
- An unmapped sub-account on its own page sees a friendly card explaining setup is in progress.
- Money shown in the admin is an estimate: `minutes × current rate`, labelled as such. Phase 3
  owns the real `Bill` and `BillLine` models, which apply per-call/per-day rates.

**Period filter** (`MonthPicker.tsx`): on the client page, arrows and a dropdown to select a month.
Bounded at the current month (no future billing) and at the oldest month in the list (so the
selected value always exists). Drives both the detail request and the rendering of all figures.

---

## 6. Task 4: voice rebilling (Phases 1, 2 and 2b built)

### 5.1 The business rule

Retell and Twilio each bill Jerry for everything as one bill. We split that usage per client (per
GHL sub-account), apply the rates Jerry set for that client, and bill each client separately from
their $397 plan. Example: Cool Breeze, 200 AI minutes at $0.25 plus 100 regular call minutes at
$0.05 = a $55 usage bill.

**The two ingest paths share exactly one thing: `ClientMapping`.** Retell is one row per call,
written once. Twilio is one row per client per GMT day, re-written on every pull. Do not unify
them. Do not apply Retell's rounding rule to Twilio.

### 5.2 Models (`apps/rebilling/models.py`)

| Model | Purpose | Key fields |
|---|---|---|
| `ClientMapping` | One client. Links the GHL sub-account to Retell agents, phone numbers and the Twilio subaccount | `location_id` (unique), `client_name`, `retell_agent_ids` (JSON list), `phone_numbers` (JSON list, digits-only match), `twilio_subaccount_sid`, `billing_mode` (`auto_charge` default, or `send_invoice`), `stripe_customer_id`, `billing_contact_id`, `active` |
| `RateHistory` | Rates per client from a start date. A change **adds a row** | `voice_rate_cents` (default 25 = Retell AI minute, $0.25), `call_rate_cents` (default 5 = Twilio call minute, $0.05), `start_date`. Unique on `(client, start_date)` |
| `RetellCall` | One Retell call matched to a client | `call_id` (unique), `client` (FK, **PROTECT**), agent, numbers, direction, `started_at`, `ended_at`, `duration_ms`, `retell_cost_cents` (= `call_cost.combined_cost`, Jerry's cost), `call_status`, `billing_status`, `unbillable_reason` |
| `UnassignedCall` | Same call fields, no client. Matched nothing. Never dropped | same as `RetellCall` minus `client` |
| `TwilioUsageDay` | One client, one GMT day, one category | `client` (FK, **PROTECT**), `subaccount_sid`, `usage_date`, `category` (`calls`), `call_count`, `usage_minutes` (Decimal, already rounded by Twilio), `twilio_price_cents` (Jerry's cost), `as_of` (Twilio's freshness stamp). Unique on `(client, usage_date, category)` |

Migrations: `0001_initial`, `0002_call_billing_status`, `0003_twilio_voice_usage`. After any model
change, `makemigrations --check --dry-run` should report "No changes detected".

### 5.3 Pure functions that carry the rules

- `billable_minutes(duration_ms)` returns per-second (`Decimal`) or round-up-to-minute, from
  `RETELL_ROUNDING`. Retell only.
- `billing_state(duration_ms, started_at, call_status)` returns `(billing_status, reason)`. A call
  is **unbillable** when its status isn't `ended`, when it has no duration, or when it has no start
  time. Each case has a plain-English reason. It runs on every store and every re-read.
- `normalise_number(value)` keeps digits only, for phone matching.

### 5.4 Retell path

**Webhook** `POST /api/rebilling/webhooks/retell/` (`apps/rebilling/views.py`):

1. Reads `request.body` **before anything parses the stream**. The signature covers the raw bytes.
2. `retell.lib.verify(raw_body, RETELL_API_KEY, X-Retell-Signature)`. Returns 401 if the
   signature is bad, 400 if the JSON is bad.
3. Only `call_ended` is handled. It calls `store_call()`.
4. Always answers **204 with an empty body**. Retell allows 10 seconds and retries up to 3 times
   on a non-2xx.

**Matching** (`match_client()`): by `agent_id` first, since each client has their own agent. Then
by phone number: `to_number` for inbound, `from_number` for outbound, compared digits-only. Only
`active` mappings match. No match creates an `UnassignedCall` and logs a warning. Web calls have no
phone numbers, so they can only match by agent.

**Storing** (`store_call()`): idempotent on `call_id` across **both** tables. Retell's retries and
the nightly audit both rely on this. First write wins.

**Nightly audit** `RetellReconciliationService.reconcile()`:

- `POST /v3/list-calls` over the last **48 hours** (`RECONCILE_WINDOW_HOURS`), `limit` 1000,
  paging on `pagination_key` until `has_more` is false. It stops if `has_more` is true but no key
  comes back.
- Every call goes through `store_call()`.
- Backfill: for calls **inside the window** that are unbillable or missing a cost, it calls
  `GET /v2/get-call/{call_id}`, refreshes duration, cost, status and `ended_at`, and re-runs
  `billing_state()`.
- Errors: a failed list (429 included) raises `RetellError` and ends the run. The next night's
  window covers it. A failed get-call is logged and skipped.
- Auth: `Authorization: Bearer RETELL_API_KEY`.
- Duration is read from `duration_ms`, else `duration_seconds`, else
  `call_cost.total_duration_seconds`.

**Why the backfill is limited to the window:** a call that never connected has no duration. Without
the window it would be re-fetched every night, forever.

### 5.5 Twilio path (Phase 2b)

**Pull** `TwilioUsageService.pull(start_date=None, end_date=None)`:

- For each `active` client with a `twilio_subaccount_sid`:
  `GET https://api.twilio.com/2010-04-01/Accounts/{sid}/Usage/Records/Daily.json` with
  `Category=calls`, `IncludeSubaccounts=false` (**Twilio's default is true**, so it must be sent),
  `PageSize=1000`, `StartDate` and `EndDate`. Pages follow Twilio's `next_page_uri` verbatim.
- Auth is **HTTP Basic with the parent account's SID and Auth Token** (`TWILIO_ACCOUNT_SID`,
  `TWILIO_AUTH_TOKEN`). A main-account **API key cannot read subaccount resources**. The spec says
  otherwise (CriticalFindings T1).
- Stored with `update_or_create` on `(client, usage_date, category)`. This is the opposite of
  Retell's first-write-wins, because Twilio revises a day after it ends.
- Default window: a rolling **3 days** (`TWILIO_WINDOW_DAYS`), today included. For a month-close,
  pass both dates.
- Returns `{clients, days, updated, failed}`. `failed` exists so that "no usage" is never confused
  with "every request failed", which would silently bill zero.
- Errors are **per client**. One client's failed request, or a failed page, is logged and that
  client is skipped. The others still land.
- `_usage_minutes` and `_price_cents` parse Twilio's **string** numbers with `Decimal`, never
  `float`. The price is stored as an absolute value, rounded half-up to cents.

**Why only `calls`:** Twilio defines `calls` as "Does not include client or SIP calls". Retell
attaches to Twilio numbers through **Elastic SIP Trunking**, which lands in
`trunking-origination` and `trunking-termination`. So billing `calls` alone cannot double-count a
Retell call. Do **not** bill `calls-inbound` or `calls-outbound`. They are subsets of `calls`.

**Why Twilio minutes are not rounded again:** Twilio rounds each call to its billed increment
before summing into the day. Applying `RETELL_ROUNDING` on top would round twice.

### 5.6 Schedules and commands

| What | Schedule (UTC) | Celery task | Manual command |
|---|---|---|---|
| Retell audit | 03:00 daily | `apps.rebilling.tasks.reconcile_retell_calls` | `python manage.py reconcile_retell_calls` |
| Twilio pull | 03:30 daily | `apps.rebilling.tasks.pull_twilio_usage` | `python manage.py pull_twilio_usage [--start YYYY-MM-DD --end YYYY-MM-DD]` |

- The schedule lives in `CELERY_BEAT_SCHEDULE` in `config/settings/base.py`.
- `config/celery.py` is the Celery app. `config/__init__.py` exposes it so `@shared_task` binds.
- Deploying needs **both** `celery -A config worker` and `celery -A config beat`, plus a reachable
  Redis (`REDIS_URL`). Nothing runs without them.
- The Retell command exits non-zero on failure. The Twilio command exits non-zero **only when every
  client fails**, which points to a systemic problem such as bad credentials or Twilio being down.
  A partial failure warns and exits 0. Cron can monitor the exit code.

### 5.7 Rebilling settings

| Setting | Default | Notes |
|---|---|---|
| `RETELL_API_KEY` | none | Must be the key with the **webhook badge**. It also verifies signatures |
| `RETELL_ROUNDING` | `per_second` | or `round_up_minute`. **Retell only** |
| `RETELL_API_BASE_URL` | `https://api.retellai.com` | |
| `RETELL_REQUEST_TIMEOUT` | 30 | seconds |
| `TWILIO_ACCOUNT_SID` | none | The **parent** account's SID |
| `TWILIO_AUTH_TOKEN` | none | The **parent** account's auth token. A plain env setting, not encrypted in the database (decided; see plan.md) |
| `TWILIO_API_BASE_URL` | `https://api.twilio.com` | |
| `TWILIO_REQUEST_TIMEOUT` | 30 | seconds |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker |

### 5.8 Phase status

| Phase | What | Status |
|---|---|---|
| 1 | Models, client mapping, Retell webhook | Built |
| 2 | Retell nightly reconciliation (list + backfill) | Built |
| 2b | Twilio daily usage pull (`calls` only) | Built. Latest commit `ea95a0c` |
| 3 | Period calculation and bills, both sources, typed bill lines, integer cents | **Not started. Not requested** |
| 4 | Admin page: rates, mapping, unassigned calls, bills, usage split by source | Not started |
| 5 | Charging: GHL invoice schedule with auto-payment, then Stripe, then an invoice-link fallback | Not started |
| 6 | Alerts (failed pulls, unassigned calls, failed charges, overdue bills), then the Branwynn end-to-end test | Not started |

**Nothing reads `TwilioUsageDay` or `call_rate_cents` yet.** Phase 3 is where that happens, and
it must use `billing_status = billable` rows only.

---

## 6. Financial safeguards

These are enforced in code, or are explicit requirements for the next phases.

1. **Never drop a call.** Anything that can't be billed is stored as `billing_status = unbillable`
   with a plain-English `unbillable_reason`.
2. **The default is unbillable.** A row created by hand in the shell is never billed until it has
   been checked.
3. **Duration comes from three places** (`duration_ms`, `duration_seconds`, `call_cost`), so a
   call isn't lost to the unit it arrived in.
4. **A missing cost does not block billing.** `retell_cost_cents` is Jerry's cost, used for margin
   only. It is not a client-facing input.
5. **Integer cents for stored money.** `Decimal` for minutes. Never `float` for money. Twilio's
   string numbers are parsed as `Decimal`.
6. **Rounding is per source.** `RETELL_ROUNDING` applies to Retell only. Twilio arrives pre-rounded.
7. **No double-billing.** Bill Twilio `calls` only. Retell lands in the trunking categories.
8. **Re-pulls are safe.** Twilio uses `update_or_create`. Retell uses first-write-wins on `call_id`.
9. **Never charge the same period twice** (Phase 3 to 5 requirement: one bill per client per
   period, and check its status before any retry).
10. **Save the bill and its lines before charging**, so any number can be explained later.
11. **Rates have history.** A rate change adds a `RateHistory` row, and usage before the change keeps
    the old rate. Phase 3 must look up each rate by the day of the call or the `usage_date`.
12. **A failed Twilio pull must not look like zero usage.** The `failed` count and the command's
    non-zero exit exist for this. Phase 6 should alert on it.
13. **A failed page abandons that client's whole window.** A half-read window would look settled.

---

## 7. Money model for Phase 3 (planned; read before building)

- A monthly billing period, kept as a setting.
- Per active client, per period:
  - **AI-minute line:** sum the `billable_minutes` of `billable` `RetellCall` rows, multiplied by
    `voice_rate_cents` in effect on each call's day.
  - **Call-minute line:** sum `TwilioUsageDay.usage_minutes`, multiplied by `call_rate_cents` in
    effect on each `usage_date`.
- All arithmetic in integer cents, with typed bill lines so Retell and Twilio stay distinguishable.
- Margin comes from `retell_cost_cents` and `twilio_price_cents`, both already stored.
- Clients with nothing to bill are skipped.
- The bill and its lines are saved before any charge.
- New models: `Bill` and `BillLine`. `RetellCall` and `TwilioUsageDay` gain a reference to the bill
  they were billed on.

---

## 8. Frontend

- `frontend/src/App.tsx` holds the routes and `main.tsx` mounts them. The Vite config proxies
  `/api` to `:8000`.
- Built: the `/onboarding` page (connect button, a status banner for connected or error, the list
  of sub-accounts, and a sync button). Tests are in `OnboardingPage.test.tsx`.
- Not built: the agency dashboard, the client dashboard, and the admin page.
- `frontend/dist/` is build output and is gitignored.
- **Not verified in the recent sessions:** the frontend test suite and lint. Run `npm test` and
  `npm run lint` before relying on them.

---

## 9. Environment variables

Everything is in `backend/.env`, which is gitignored. `backend/.env.example` documents every key.
**Never print, log or commit `.env`.** The keys it needs:

- **Django:** `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `FRONTEND_URL`,
  `DATABASE_URL` (defaults to SQLite), `REDIS_URL`.
- **Encryption:** `FIELD_ENCRYPTION_KEY` (a Fernet key). Generate it with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  **Rotating it makes every stored token unreadable**, so it needs a plan first.
- **GHL:** `GHL_CLIENT_ID`, `GHL_CLIENT_SECRET`, `GHL_APP_ID`, `AGENCY_LOCATION_ID`,
  `GHL_REDIRECT_URI` (`http://localhost:8000/api/oauth/callback/`), `GHL_INSTALL_URL_BASE`,
  `GHL_API_VERSION` (default `2021-07-28`), `GHL_SCOPES`, `GHL_API_BASE_URL`,
  `GHL_REQUEST_TIMEOUT`.
- **Retell:** `RETELL_API_KEY`, `RETELL_ROUNDING`, `RETELL_API_BASE_URL`, `RETELL_REQUEST_TIMEOUT`.
- **Twilio:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_BASE_URL`,
  `TWILIO_REQUEST_TIMEOUT`.

`GHL_COMPANY_ID` was **removed entirely**. Do not reintroduce it. `AGENCY_LOCATION_ID` replaced it
as the way to identify the agency location.

---

## 10. Commands

Run these from `backend/` unless noted. The venv is `backend/.venv`. On Windows, use
`.venv/Scripts/python.exe`.

```
pytest                                                    # 154 tests, config.settings.test
ruff check . && ruff format .                             # lint / format (line 100, single quotes)
python manage.py check
python manage.py makemigrations --check --dry-run         # must say "No changes detected"
python manage.py migrate
python manage.py runserver                                # http://localhost:8000
python manage.py reconcile_retell_calls                   # manual Retell audit
python manage.py pull_twilio_usage                        # manual Twilio pull (rolling window)
celery -A config worker -l info                           # background worker
celery -A config beat -l info                             # scheduler
pip-sync requirements/dev.txt                             # install dependencies
pip-compile requirements/base.in -o requirements/base.txt # after editing a .in file
```

From `frontend/`: `npm install`, `npm run dev` (http://localhost:5173), `npm test`,
`npm run build`, `npm run lint`.

**Test settings** (`config/settings/test.py`) set fake values for every secret, including
`AGENCY_LOCATION_ID='agency-loc'`, `RETELL_API_KEY='retell-test-key'`,
`TWILIO_ACCOUNT_SID='ACparent'` and `TWILIO_AUTH_TOKEN='twilio-test-token'`.

---

## 11. Testing rules

- **No live HTTP calls to GHL, Retell, Twilio or Stripe, in tests or in scripts.** Every external
  call is mocked with `respx` or a test double. The user has said this explicitly and repeatedly.
  Do not run anything that could reach a real API.
- Tests run against in-memory SQLite and a fixed Fernet key.
- Shared helpers: `apps/rebilling/tests/test_services.py` (`make_client`, `call_payload`) is reused
  by the other rebilling test modules. `test_reconciliation.py` has `recent_call()`, which builds
  timestamps inside the 48-hour window. The fixed-date payload is deliberately **outside** it.
- Current count: **154 backend tests, all passing**. Tests added for the GHL v3 header fix,
  decoupled sync failures, and sub-account storage without tokens.

| File | Covers |
|---|---|
| `apps/ghl/tests/test_services.py`, `test_views.py` | OAuth exchange, refresh, location tokens, sync, callback |
| `apps/rebilling/tests/test_services.py` | matching, `store_call`, idempotency, billing state, rounding |
| `apps/rebilling/tests/test_views.py` | webhook signature, 204, 400, 401, event filtering |
| `apps/rebilling/tests/test_reconciliation.py` | list and paging, backfill, errors, command |
| `apps/rebilling/tests/test_tasks.py` | Retell Celery task, beat schedule |
| `apps/rebilling/tests/test_twilio.py` | Twilio pull, paging, update vs insert, per-client failure, command, Twilio task, schedule |
| `frontend/src/features/onboarding/OnboardingPage.test.tsx` | onboarding page (not run recently) |

---

## 12. Engineering rules

`CLAUDE.md` has the full list. The essentials:

- **Build only what is asked.** No extra endpoints, fields, settings, models, admin pages or
  "for later" features. If something seems missing, list it as a suggestion and don't build it.
- **Data models are the user's decision.** Use exactly the fields given. Don't add timestamps,
  status enums, JSON blobs, flags or helper tables without asking.
- **One code path.** No fallbacks for hypothetical API variants. Pick the documented option.
- **No speculative abstractions.** No base classes, mixins, custom QuerySets or helper modules until
  there are two real users. A plain `.filter()` is fine.
- **Small error handling.** One exception per real caller distinction.
- **Security:** secrets are encrypted and never appear in URLs or responses. Don't add auth layers
  or signatures that weren't requested. Mention risks in the summary instead.
- **Tests cover what was asked**, not features that weren't requested.
- **When in doubt, do less and say what was left out.**
- Follow the shape of the reference projects: `D:\tasks\Longevity\` and
  `D:\tasks\Proinsight projects\product_updates_automation\`. Use official docs for API facts only.
- **Working conventions:** don't commit or push unless asked. Don't touch files outside a task's
  stated scope. Don't start a new phase without being asked.

---

## 13. Findings that changed the design

Full detail is in `local_folder/CriticalFindings.md`. These are the ones that would bite a new
session:

- **Twilio API keys cannot read subaccount usage.** Use the parent SID and auth token. The PDF
  says otherwise.
- **Twilio usage minutes are pre-rounded.** Never apply `RETELL_ROUNDING` to them.
- **Twilio's `IncludeSubaccounts` defaults to `true`.** Always send `false`.
- **Retell's `call_ended` docs** define it as "all fields except `call_analysis`", which implies
  `duration_ms` and `call_cost` are present. The sample payload doesn't show them. Verify on the
  first real call.
- **Retell's SDK has no `Retell.verify` class method.** The function is `retell.lib.verify`.
- **Retell `call_status` has five values:** `registered`, `not_connected`, `ongoing`, `ended`,
  `error`. Only `ended` is billable.
- **`call_analysis` can arrive later than `call_ended`.** Use `call_analyzed` if a future phase
  needs analysis data.
- **GHL `companyId` comes from the token response**, not from config.
- **The GHL location token endpoint** (`/oauth/locationToken` vs `/oauth/location-token`) differs
  between doc generations. Verify on Jerry's account.
- **GHL's `Version` header** is the real API-version mechanism, not the "v2" or "v3" label in the
  prose.
- **The GHL SaaS subscription response format** is undocumented. It blocks Task 2's MRR KPI until
  it is tested on Jerry's account.

---

## 14. Open questions and live verification

These cannot be answered from the docs.

**Blocks trusting Twilio at all:**

- Is every client on **their own Twilio subaccount**, as Branwynn is? If not, Usage Records cannot
  attribute calls to a client. The fallback is per-call `Calls.json` matched by phone number, a
  materially different build (described in `plan.md`).
- Jerry must supply the **parent Account SID and Auth Token**, or grant an admin invite. A
  main-account API key is not enough.

**Verify with one real day of Twilio data:**

- Retell minutes appear only under the `trunking-*` categories, never under `calls`.
- Every Retell-connected number is actually on the Elastic SIP trunk. A number connected any other
  way bills as a regular staff call.
- Twilio's `usage` for `calls` is whole minutes, already rounded per call.

**Verify on the first real Retell call:**

- `call_ended` carries `duration_ms` and `call_cost`.
- No agent has its own `webhook_url` overriding the account-level one.
- Each agent's `webhook_events` includes `call_ended`.
- `RETELL_API_KEY` is the webhook-badge key. Otherwise signatures fail silently.

**Verify on Jerry's GHL account:**

- The `Version` header, the `locationToken` path casing, `installed-locations` pagination, and the
  white-label install host (`GHL_INSTALL_URL_BASE`).
- The SaaS subscription response shape (`GET /saas/get-saas-subscription/{locationId}`).
- Whether a card saved on a **master sub-account contact** can be auto-charged through an invoice
  schedule (Phase 5, Option 1). The GHL team has been asked.

**Business questions for Jerry:**

- Is billing **inbound as well as outbound** regular calls intended? `calls` covers both, so a
  client pays for calls their own customers make to them.
- How are **transfer legs** billed? `call_cost.product_costs[].is_transfer_leg_cost` marks them.
  Currently the whole `duration_ms` is billed.
- Do clients pay **monthly or prepaid**? The backend PDF says "being confirmed".
- Who receives **alert emails**? (Phase 6.)
- If SMS ever comes into scope: does Twilio's `usage` count messages or **segments**?

---

## 15. Known gaps and risks

- **Nothing runs in production yet.** A worker and a beat process must be deployed first.
- **A failed nightly run only logs.** There is no alert until Phase 6. Until then, a silently
  failing Retell audit or Twilio pull under-bills with no visible signal.
- **No Django admin** for the rebilling or GHL models. Mappings are created from the Django shell.
- **The install check is weaker** (see section 4). Any agency that satisfies the "Company" type can
  install. (A marketplace app being private limits the exposure.)
- **Refresh-token locking assumes one database.** It is fine on a single Postgres. Revisit it
  before running multiple app instances.
- **The 48-hour Retell window is a constant**, not a setting. A correction older than 48 hours is
  never caught.
- **`TWILIO_AUTH_TOKEN` sits in `.env`.** If the server is compromised, Twilio's master credential
  is exposed. The only mitigations are file permissions and the gitignore.
- **`local_folder/` is gitignored.** `plan.md` and `CriticalFindings.md` exist **only on this
  machine**. Commit `6d0b7b1` claims `plan.md` is in it, and it is not.
- **`apps/dashboards` is empty**, and the `frontend/src/features/*-dashboard` folders hold only
  placeholders.
- **The admin page is built but not verified in a browser yet.** SSO handshake logic is tested,
  and all backend endpoints pass their tests, but the React UI rendering and the GHL iframe
  embedding have not been verified live.

---

## 16. Recent session: GHL location sync fixes and admin page

**GHL `/oauth/installed-locations` 404 error (fixed):**
- The endpoint was returning 404 under the default `Version: 2021-07-28` header.
- `_installed_locations()` now sends `Version: v3` (GHL v3 API required).
- `_request()` now accepts an optional `version` parameter to override per-call.

**Decoupled OAuth callback (fixed):**
- `exchange_code()` and `sync_locations()` are now in separate try-catch blocks.
- A sync failure (e.g., app not installed on a sub-account) no longer fails the whole install.
- Sync logs the error with GHL's reason and continues; install still redirects with
  `status=connected`.

**Improved error diagnostics:**
- `_request()` catches `httpx.HTTPStatusError` separately and includes the response body in
  the error message (capped at 500 chars), so GHL's 400/500 reasons are visible in logs.

**Sub-account visibility without tokens (fixed):**
- Removed `isInstalled: 'true'` filter from `_installed_locations()`, so **all** sub-accounts
  under the agency are fetched, not just ones with the app installed.
- When `_mint_location_token()` fails, the sub-account is stored anyway with `access_token=''`
  and a future `expires_at`, so it appears in the admin dashboard even before the app is
  installed on it.

**Frontend admin page (built):**
- Route `/admin/` is live; requires valid GHL SSO and reads real sub-accounts + rates + usage.
- Route `/admin/preview` is standalone demo data; development only, never calls the API.
- Both share rendering via `ClientBrowser.tsx`, `AdminPage.tsx`, and `AdminPreviewPage.tsx`.
- Unmapped sub-accounts on their own page see a friendly "setup in progress" card, not a 404.
- SSO scope resolved from `activeLocation` matches `AGENCY_LOCATION_ID` correctly.

**Test count:** 154 passing (up from 110). New tests verify v3 header, decoupled sync failure,
sub-account storage without tokens, and error message propagation.

---

## 17. Git history

| Commit | Message (abridged) |
|---|---|
| `da0ddb3` | GHL Onboarding |
| `9e7f703` | Minor update, adding location id in .env, and removing company id |
| `6d0b7b1` | feat(rebilling): implement Phase 1 models, Retell webhook, and call matching |
| `44c3d94` | feat(rebilling): implement Phase 2 Retell nightly reconciliation audit |
| `d9ab56a` | feat(rebilling): add unbillable call tracking and Celery daily audit schedule |
| `ea95a0c` | Integrated twillio calls with Retell. This is the Phase 2b work |

The working tree has uncommitted changes from the most recent session:
- GHL API v3 header fix for `/oauth/installed-locations`
- Decoupled sync failures from install failures
- Sub-account storage without tokens
- Frontend admin page (`/admin`, `/admin/preview`), SSO scope resolution, and unmapped states

---

## 18. Memory and collaboration

- Claude's persistent memory for this project is indexed by `MEMORY.md` and currently holds one
  entry: **avoid over-engineering**. Build exactly what is asked, with no speculative extras.
- The user pastes multi-part specs and expects each part to be worked through in full, with
  anything not done called out explicitly.
- The user wants short, direct summaries: what changed, what was verified, and what was deliberately
  left out.

---

## 19. How to start a new session on this project

1. Read this file, then `CLAUDE.md`.
2. If the task touches rebilling, read `local_folder/plan.md` and `local_folder/CriticalFindings.md`.
3. Confirm the baseline. From `backend/`, run `.venv/Scripts/python.exe -m pytest -q` and expect
   **154 passed**. Also run `ruff check .` and `manage.py makemigrations --check --dry-run`.
4. Check `git status` and `git log --oneline | head` before changing anything.
5. Don't start Phase 3 or Tasks 2 and 3 without an explicit request. Ask first if the request is
   ambiguous. Never make live API calls.
