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
| 1b | Agency admin page: client list and usage by period | — | **Built** (`/admin` live, `/admin/preview` demo; read-only for usage, editable for mapping) |
| 2 | Agency dashboard: MRR, live clients, leads/calls/signed by source, clients at risk, voice revenue | Oct 30 | **Not started** (`apps/dashboards` is an empty scaffold) |
| 3 | Client dashboard: one client's leads, booked jobs, response time, voice usage | Nov 4 | **Not started** (frontend feature folders hold only `.gitkeep`) |
| 4 | Voice rebilling: Retell + Twilio voice, split per client | Nov 13 | **Phases 1, 2, 2b, 3 (calculation), 4 (mapping edit UI) and 5 (dual-rail charging: GHL invoice + Stripe fallback) built**; rest of Phase 4 (rate editing, unassigned-call mapping, bills in the UI) and Phase 6 not started |

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
| POST | `/api/rebilling/webhooks/stripe/` | Stripe invoice event webhook (`invoice.paid`, `invoice.payment_failed`). Signature-verified, answers 204 |
| — | `/admin/` | Django admin. `ClientMapping` is registered; GHL models have no admin registrations yet |

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

## 5. Agency admin page (Task 4 Phase 4, brought forward; partially built)

The embedded admin interface Jerry opens from the GHL left nav: onboarded clients, their rates and
voice usage by period. Agency admins can view and edit client mappings (Retell Agent IDs, phone
numbers, Twilio subaccount SID). Sub-accounts viewing their own page see mapping read-only. This is
all Phase 4 work (plan.md calls the viewing half "the read-only half of Phase 4, brought forward";
mapping editing is Phase 4's own write half) — built ahead of Phase 3's calculation engine rather
than after it, which is why it was previously (incorrectly) referred to as "Phase 3" in this file.
Editing rates, mapping unassigned calls, and showing real bills are the rest of Phase 4 and are not
built yet.

**Routes:** `/admin/` is live data (requires GHL SSO, calls the real API); `/admin/preview` is bundled sample data
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

**Endpoints** (all require valid SSO blob in `X-GHL-Session`, except the last):
- `POST /api/ghl/sso/session/` — who is viewing (user name, email, role, scope, active location).
- `GET /api/rebilling/admin/clients/` — list of all sub-accounts (left-joined to `ClientMapping`),
  with `month=YYYY-MM` filter. An unmapped client shows `mapped: false` (not an error).
- `GET /api/rebilling/admin/clients/<location_id>/` — detail for one client, with usage and rates.
  Returns `{month, location_id, client_name, mapped: false}` (no usage fields) for an unmapped
  sub-account viewing its own page; agency scope requests for unmapped clients still 404 because
  the list already marked them unmapped and there is nothing to show.
- `PUT /api/rebilling/admin/clients/<location_id>/mapping/` — agency admins only. Set or edit a
  client's mapping (Retell agent IDs, phone numbers, Twilio subaccount SID). Persists to the database.
  Sub-account sessions (location scope) receive 403 Forbidden. Unmapped or onboarded clients
  receive 404. Returns the updated mapping fields.

**Frontend** (`features/admin/AdminPage.tsx`, `AdminPreviewPage.tsx`, `ClientBrowser.tsx`, `MappingEditor.tsx`):
- Unframed pages (no GHL parent, or the SSO handshake failed) render a message linking to
  `/admin/preview` instead of showing an error.
- The preview page (`AdminPreviewPage.tsx`) renders bundled sample data (`demoData.ts`) behind an
  amber "Standalone preview" banner and **never calls the admin API**. It is for development and
  walkthrough only, not for live testing inside GHL.
- Both live and preview pages use `ClientBrowser.tsx` for list/detail rendering, so they are
  visually identical.
- **Mapping editing:** `MappingEditor.tsx` is a reusable component (Retell agent IDs, phone numbers,
  Twilio subaccount SID) that appears in two places: on a mapped client's detail page (editable for
  agency admins, read-only `<dl>` for sub-accounts), and on an unmapped client's page as a "Set up
  for billing" flow (agency admins only). When saved on the live page (`/admin`), it calls
  `PUT /api/rebilling/admin/clients/<location_id>/mapping/`. In the preview page (`/admin/preview`),
  it writes to an in-memory `previewMappings` store (`demoData.ts`) with zero network calls.
- An unmapped sub-account on its own page sees a friendly card explaining setup is in progress
  (not editable).
- Money shown in the admin is still an estimate: `minutes × current rate`, labelled as such. Phase
  3 (built; see §5.8) now produces real `Bill`/`BillLine` rows at the rate in effect on each call's
  day or each usage date, but this page does not read them yet — switching this page to show real
  bills instead of the estimate is part of what remains in Phase 4.

**Period filter** (`MonthPicker.tsx`): on the client page, arrows and a dropdown to select a month.
Bounded at the current month (no future billing) and at the oldest month in the list (so the
selected value always exists). Drives both the detail request and the rendering of all figures.

---

## 6. Task 4: voice rebilling (Phases 1–5 built; Phase 6 pending)

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
| `ClientMapping` | One client. Links the GHL sub-account to Retell agents, phone numbers and the Twilio subaccount | `location_id` (unique), `client_name`, `retell_agent_ids` (JSON list), `phone_numbers` (JSON list, digits-only match), `twilio_subaccount_sid`, `billing_mode` (`auto_charge` default, or `send_invoice`), `stripe_customer_id`, `billing_email`, `billing_contact_id`, `active` |
| `RateHistory` | Rates per client from a start date. A change **adds a row** | `voice_rate_cents` (default 25 = Retell AI minute, $0.25), `call_rate_cents` (default 5 = Twilio call minute, $0.05), `start_date`. Unique on `(client, start_date)` |
| `RetellCall` | One Retell call matched to a client | `call_id` (unique), `client` (FK, **PROTECT**), agent, numbers, direction, `started_at`, `ended_at`, `duration_ms`, `retell_cost_cents` (= `call_cost.combined_cost`, Jerry's cost), `call_status`, `billing_status`, `unbillable_reason` |
| `UnassignedCall` | Same call fields, no client. Matched nothing. Never dropped | same as `RetellCall` minus `client` |
| `TwilioUsageDay` | One client, one GMT day, one category | `client` (FK, **PROTECT**), `subaccount_sid`, `usage_date`, `category` (`calls`), `call_count`, `usage_minutes` (Decimal, already rounded by Twilio), `twilio_price_cents` (Jerry's cost), `as_of` (Twilio's freshness stamp). Unique on `(client, usage_date, category)` |

Migrations: `0001_initial`, `0002_call_billing_status`, `0003_twilio_voice_usage`, `0004_bill_and_billline`, `0005_bill_stripe_fields_and_billing_email`. After any model
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

### 5.8 Phase 3 — calculation (built)

`calculate_bills(month=None)` (`apps/rebilling/services.py`) and `python manage.py calculate_bills
[--month YYYY-MM]`. One `Bill` row per active client per `billing_period` (`YYYY-MM`, DB-level
`unique_together(client, billing_period)` so the same period is never billed twice), with a
`BillLine` per source that actually has billable usage.

- **AI-minute line:** sums `billable_minutes` over `RetellCall` rows with `billing_status =
  billable` in the month, each at the `voice_rate_cents` in effect **on that call's day** — a
  mid-month rate change splits the bill correctly. A call on a day with no `RateHistory` row yet
  is logged and left out of this bill (not marked billed), rather than failing the whole run.
- **Call-minute line:** sums `TwilioUsageDay.usage_minutes` in the month, each at the
  `call_rate_cents` in effect **on that `usage_date`**. Never rounded again — Twilio's minutes
  arrive pre-rounded.
- A client with no `twilio_subaccount_sid` simply gets no Twilio line; Retell billing for everyone
  else is unaffected.
- All arithmetic in `Decimal`; the final cents total is rounded once with `ROUND_HALF_UP` (the same
  convention `usage_summary()`'s estimate already uses), never truncated and never float.
- A client with nothing billable from either source gets no `Bill` row at all — never a zero bill.
- `RetellCall.bill` / `TwilioUsageDay.bill` are set only on the rows actually included in a bill.
  Both are `on_delete=SET_NULL`: deleting a `Bill` (e.g. a correction) unlinks those rows but never
  deletes the underlying call or usage-day records.
- Rate lookup fetches a client's `RateHistory` once per billing run, not once per call — a month
  can hold thousands of calls, and the rate changes only a handful of times at most.
- **Deliberately not automated:** the plan calls for running Twilio's month-close pull
  (`pull_twilio_usage --start --end`) before calculating, so the bill is built on settled days.
  `calculate_bills` does not trigger this itself — it stays two separate commands, run in order,
  rather than coupling the two services.
- **Deliberately not built here:** no admin/API exposure of bills (Phase 4), no charging (Phase 5).

**Tests** (`apps/rebilling/tests/test_billing.py`, 19 tests): rate-in-effect billing, mid-month
rate splits, unbillable-call exclusion, Twilio no-double-rounding, single-source clients
(Retell-only or Twilio-only), zero-usage clients skipped, inactive clients never billed, idempotent
re-run of an already-billed month, two clients billing independently, the integer-cents invariant,
`SET_NULL` on bill deletion, and the management command (success output and bad-month rejection).

### 5.9 Phase 4 — status and mapping edit UI (built)

| Phase | What | Status |
|---|---|---|
| 1 | Models, client mapping, Retell webhook | Built |
| 2 | Retell nightly reconciliation (list + backfill) | Built |
| 2b | Twilio daily usage pull (`calls` only) | Built. Latest commit `ea95a0c` |
| 3 | Period calculation and bills, both sources, typed bill lines, integer cents | **Built** (§5.8) |
| 4 | Admin page: rates, mapping edit UI, unassigned calls, bills, usage split by source | **Mapping edit UI built** (live and preview, admin registration, Django admin for mapping); rate editing, unassigned-call mapping and showing real bills not started |
| 5 | Charging: GHL invoice with auto-payment, Stripe fallback after 72h, webhooks for both | **Built** (§5.10, §24) |
| 6 | Alerts (failed pulls, unassigned calls, failed charges, overdue bills, **bills stranded by a GHL invoice that would not void**), then the Branwynn end-to-end test | Not started |

**Backend (Phase 4 mapping UI):** `ClientMappingSerializer` validates Retell agent IDs (non-blank,
deduplicated), phone numbers (7–15 digits, deduplicated, normalized), and Twilio subaccount SID
(regex `^AC[0-9a-fA-F]{32}$`, blank allowed). `set_mapping()` service creates or updates a mapping,
persisting only the fields sent (leaves others untouched). `AdminClientMappingView` enforces agency
scope (403 for sub-accounts, 404 for unmapped clients). `ClientMapping` is now registered in Django
admin (`list_display`, `list_filter`, `search_fields`). All 12 new tests pass: valid saves, partial
updates, field dedupe, validation rejections, auth failures.

**Frontend (Phase 4 mapping UI):** `MappingEditor.tsx` (new) is a unified editor reused across live
and preview. Props: `locationId`, `clientName`, `initial`, `preview` (boolean), `onSaved` callback.
Renders labeled inputs for the three mapping fields, with a chip-list UI for agent IDs and phone
numbers (add on Enter or button click, remove via ×). On save: if `preview=true`, calls
`savePreviewMapping()` (in-memory, no network); else calls `saveMapping()` (live API). Error handling:
400 → user-friendly "Check the Twilio subaccount SID and the phone numbers"; else → "Could not save.
Try again." The component is threaded into `ClientDetail.tsx` (edit or read-only `<dl>` based on
`canEdit = !isSubAccount`), and into `ClientBrowser.tsx`'s `SetUpMapping` flow for unmapped clients
(agency admins only). `ClientsTable.tsx` now renders unmapped rows as clickable for agency admins
(not clickable for sub-accounts). 

**Preview (in-memory mapping):** `previewMappings` module-level `Map<string, MappingFields>` in
`demoData.ts` holds edits made during a session. `savePreviewMapping()` writes to it;
`resetPreviewMappings()` clears it (called in test `afterEach` to prevent test bleed). `withPreviewMapping(spec)`
applies any edit to a demo spec (updates agents, numbers, twilio_sid). `unmappedOrSetUp()` returns
the static `UNMAPPED` summary or a mapped version if an edit exists. `demoClients()` and `demoDetail()`
read the store, so the preview UI reflects edits persistently across navigations — no API calls ever.

**Architecture decision (404 avoidance):** The live `AdminPage.tsx` skips fetching detail when the
clients-list summary already marks a row `mapped: false`, synthesizing the `{month, location_id,
client_name, mapped: false}` shape directly from the summary. This avoids the backend's 404-for-agency-on-unmapped-detail behavior (a constraint that exists to keep unmapped clients out of the list in agency views, but the list already knows they are unmapped). The frontend fix keeps all existing tests passing and allows an agency admin to open an unmapped client to set it up.

**Test count:** Backend 195 passed (up from 176: +19 Phase 3 calculation tests in `test_billing.py`).
Frontend 43 passed (unchanged this session — Phase 3 was backend-only; no frontend reads bills yet).
Linting clean; `makemigrations --check --dry-run` clean.

**`call_rate_cents` and the Twilio usage are now read for billing.** Phase 3's `calculate_bills`
does this, using `billing_status = billable` rows only. The admin page itself still shows an
estimate rather than these real bills — see §5, "Money shown in the admin."

### 5.10 Phase 5 — charging (dual-rail: GHL invoice + Stripe fallback, built)

Bills are charged through two rails in sequence: GHL invoice first, Stripe fallback after 72 hours if
GHL does not collect. The primary GHL rail reads client contacts live from `GHL_MAIN_LOCATION_ID` and
auto-charges their saved cards. If GHL's invoice never becomes `paid` after 72 hours (decline or silent
no-op; GHL publishes no charge-failed event), the invoice is voided and Stripe takes over. Stripe
handles off-session charging with idempotency keys and webhooks.

**GHL Invoice Service** (`apps/rebilling/ghl_billing.py`, new):
- `create_invoice(bill)` — raises a draft invoice in `GHL_MAIN_LOCATION_ID`, fetches contact details
  live from GHL (name, email, phone; phone falls back to `'N/A'`), writes the email back to
  `ClientMapping.billing_email` for the Stripe rail.
- `send_invoice(invoice_id)` — sends the invoice with `autoPayment: {enable: true}`, which is the
  entire mechanism for auto-charging the card on file (no `paymentMethodId` to send; GHL has no
  public endpoint to read saved cards).
- `void_invoice(invoice_id)` — voids the invoice, critical safeguard before Stripe fallback (prevents
  double-charge if GHL retries).
- `apply_event(event)` — webhook handler for `InvoicePaid` only; other invoice events are ignored.

**Stripe Billing Service** (`apps/rebilling/stripe_billing.py`, new):
- `charge_on_stripe(bill)` — creates a Stripe invoice (`collection_method='charge_automatically'`,
  `auto_advance=True`), adds line items from `BillLine` rows (amounts in integer cents), finalizes
  it (triggers card attempt), and sets bill status to `CHARGING`.
- `customer_id(client)` — looks up or creates a Stripe customer by `billing_email`. A client who
  already pays through this account is found and reused (no duplicate cardless customers). Cached
  after first lookup.
- `read_event(payload, signature)` — verifies webhook signature using `stripe.Webhook.construct_event`.
- `apply_event(event)` — processes `invoice.paid` and `invoice.payment_failed`, updating bill status
  and `paid_at`.

**Dual-Rail Orchestrator** (`apps/rebilling/dual_rail.py`, new):
- `charge_bills(month=None)` — the only entry point for charging. All `uncharged` bills for a month →
  GHL rails first (creates, sends, awaits webhook). Then a separate sweep of `charging` bills **across
  every month** older than `BILLING_FALLBACK_WINDOW_HOURS` (72 hours by default): if they are still
  `charging` (never got `InvoicePaid`), void the GHL invoice (gate: if void fails, fallback is blocked
  and bill waits for human intervention), then charge via Stripe.

**Bill status transitions:** `UNCHARGED` → `CHARGING` (GHL or Stripe invoice sent) → `PAID` (webhook
from either rail received `paid` event) or `FAILED` (webhook received `payment_failed` from Stripe,
or both rails exhausted). `paid_at` is set by webhook to provider's timestamp, not server time.

**GHL Webhook** (`POST /api/rebilling/webhooks/ghl-invoices/`, new):
- Verifies Ed25519 signature using `GHLOAuthService._verify_signature()`.
- Handles `InvoicePaid` only. Other invoice events are logged and ignored.
- Returns 204 on success, 401 on bad signature, 400 on invalid JSON.

**Stripe Webhook** (`POST /api/rebilling/webhooks/stripe/`):
- Verifies `Stripe-Signature` HMAC-SHA256 header.
- Handles `invoice.paid` and `invoice.payment_failed` events.
- Returns 204 on success, 401 on bad signature, 400 on invalid JSON.

**Management command:** `python manage.py charge_bills [--month YYYY-MM]`. Default month is this month.
Charges all `uncharged` bills for that month, then sweeps for fallback. Skips clients with
`billing_mode != auto_charge`. Logs per-client failures but continues for others. Prints summary:
GHL sent/paid/failed, Stripe fallback/paid/failed, total cents, and fallback-blocked count.

**Idempotency in Stripe:** Every Stripe call (`Invoice.create`, `InvoiceItem.create`, `finalize_invoice`)
carries an idempotency key derived from `Bill.pk`. A re-run re-reaches the same Stripe objects; only
bills still `uncharged` are picked up. Combining both guards makes the charge safe against crashes and
manual reruns.

**Escape hatch (Stripe-only mode):** `python manage.py charge_bills --stripe-only
--i-understand-this-skips-ghl` charges `uncharged` bills straight to Stripe, skipping GHL entirely.
Both flags required (neither does anything alone), so a typo or old cron job cannot trigger it.
Structured to never double-charge — it only touches `status=uncharged` bills, and bills GHL already
has live invoices for are `charging`, not `uncharged`.

**Settings:**
- `GHL_MAIN_LOCATION_ID` — the sub-account invoices are raised in (blank means no charging).
- `GHL_BUSINESS_NAME` — defaults to `'SurPro Labs AI'`; required by GHL Invoices API v3.
- `STRIPE_SECRET_KEY` — API secret for Stripe.
- `STRIPE_WEBHOOK_SECRET` — webhook signing secret (different from API key).
- `BILLING_FALLBACK_WINDOW_HOURS` — default 72; how long to wait on GHL before Stripe fallback.

**Test coverage:** 28 new tests in `apps/rebilling/tests/test_dual_rail.py`. Both rails, window
boundary, void gate, no-double-fallback, cross-month sweep, webhook events. FakeStripe mocks the
SDK; respx mocks GHL. Tests cover: successful GHL charge (no fallback), fallback after timeout,
void failure blocks fallback, Stripe success on fallback, both rails fail, escape hatch isolation,
and webhook signature verification/rejection.

**Risks documented in `CriticalFindings.md`:**
- Stripe's **$0.50 minimum charge** — a bill under 50 cents fails at the card. No policy yet.
- **GHL publishes no charge-failed webhook** — a decline and a silent no-op both arrive as `charging`
  bills that never become `paid`; age is the only signal. The 72-hour sweep exists for this.
- **GHL retries declines 2×, 24 hours apart** — cannot be switched off. The fallback window exists
  to outlast this.
- **Webhook is the single point of failure** — no reconciliation pass. A bill can stay `charging`
  forever if the webhook endpoint is wrong. Phase 6 should alert on bills stuck in `charging` for N
  days.
- **`billing_email` is unpopulated by the UI** — every client needs one filled in by hand before
  Phase 5 charging works.
- Stripe's **Smart Retries** can flip a `failed` invoice to `paid` later.

**Status:** 265 backend tests passing (257 + 8 new from location-token work). Linting, format,
migrations all clean.

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
9. **Never charge the same period twice.** Enforced at the DB level: `Bill` has
   `unique_together(client, billing_period)`. Phases 4–5 (charging) still need to check a bill's
   charge status before any retry.
10. **Save the bill and its lines before charging**, so any number can be explained later. Phase 3
    does this; nothing charges yet.
11. **Rates have history.** A rate change adds a `RateHistory` row, and usage before the change keeps
    the old rate. Phase 3's `calculate_bills` looks up each rate by the day of the call or the
    `usage_date` (built; see §5.8).
12. **A failed Twilio pull must not look like zero usage.** The `failed` count and the command's
    non-zero exit exist for this. Phase 6 should alert on it.
13. **A failed page abandons that client's whole window.** A half-read window would look settled.

---

## 7. Money model for Phase 3 (built; see §5.8 for the implementation)

- A monthly billing period (`YYYY-MM`), via the existing `month_window()` helper — not a new
  setting; the existing one already covers this.
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
  they were billed on, cleared (`SET_NULL`) rather than deleted if the bill itself is ever deleted.

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
  `GHL_MAIN_LOCATION_ID` (the sub-account holding client contacts and saved cards; Phase 5 requires
  this, blank means no charging), `GHL_BUSINESS_NAME` (defaults to `'SurPro Labs AI'`; required by
  GHL Invoices API v3), `GHL_REDIRECT_URI` (`http://localhost:8000/api/oauth/callback/`),
  `GHL_INSTALL_URL_BASE`, `GHL_API_VERSION` (default `2021-07-28`), `GHL_SCOPES`, `GHL_API_BASE_URL`,
  `GHL_REQUEST_TIMEOUT`.
- **Retell:** `RETELL_API_KEY`, `RETELL_ROUNDING`, `RETELL_API_BASE_URL`, `RETELL_REQUEST_TIMEOUT`.
- **Twilio:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_BASE_URL`,
  `TWILIO_REQUEST_TIMEOUT`.
- **Stripe:** `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`.

`GHL_COMPANY_ID` was **removed entirely**. Do not reintroduce it. `AGENCY_LOCATION_ID` replaced it
as the way to identify the agency location.

---

## 10. Commands

Run these from `backend/` unless noted. The venv is `backend/.venv`. On Windows, use
`.venv/Scripts/python.exe`.

```
pytest                                                    # 265 tests, config.settings.test
ruff check . && ruff format .                             # lint / format (line 100, single quotes)
python manage.py check
python manage.py makemigrations --check --dry-run         # must say "No changes detected"
python manage.py migrate
python manage.py runserver                                # http://localhost:8000
python manage.py reconcile_retell_calls                   # manual Retell audit
python manage.py pull_twilio_usage                        # manual Twilio pull (rolling window)
python manage.py charge_bills [--month YYYY-MM]           # manual Stripe charging (defaults to this month)
celery -A config worker -l info                           # background worker
celery -A config beat -l info                             # scheduler
pip-sync requirements/dev.txt                             # install dependencies
pip-compile requirements/base.in -o requirements/base.txt # after editing a .in file
```

From `frontend/`: `npm install`, `npm run dev` (http://localhost:5173), `npm test`,
`npm run build`, `npm run lint`.

**Test settings** (`config/settings/test.py`) set fake values for every secret, including
`AGENCY_LOCATION_ID='agency-loc'`, `RETELL_API_KEY='retell-test-key'`,
`TWILIO_ACCOUNT_SID='ACparent'`, `TWILIO_AUTH_TOKEN='twilio-test-token'`,
`STRIPE_SECRET_KEY='sk_test_key'`, and `STRIPE_WEBHOOK_SECRET='whsec_test_secret'`.

---

## 11. Testing rules

- **No live HTTP calls to GHL, Retell, Twilio or Stripe, in tests or in scripts.** Every external
  call is mocked with `respx` or a test double. The user has said this explicitly and repeatedly.
  Do not run anything that could reach a real API.
- Tests run against in-memory SQLite and a fixed Fernet key.
- Shared helpers: `apps/rebilling/tests/test_services.py` (`make_client`, `call_payload`) is reused
  by the other rebilling test modules. `test_reconciliation.py` has `recent_call()`, which builds
  timestamps inside the 48-hour window. The fixed-date payload is deliberately **outside** it.
- Current count: **221 backend tests, all passing** (195 Phase 3 + 26 Phase 5 Stripe tests).
  **43 frontend tests, all passing**. Tests added for GHL v3 header fix, decoupled sync failures,
  sub-account storage without tokens, live mapping edit/setup flows, preview in-memory editing,
  validation error handling, billing calculation, and Stripe charging with idempotency.

| File | Covers |
|---|---|
| `apps/ghl/tests/test_services.py`, `test_views.py` | OAuth exchange, refresh, location tokens, sync, callback |
| `apps/rebilling/tests/test_services.py` | matching, `store_call`, idempotency, billing state, rounding |
| `apps/rebilling/tests/test_views.py` | webhook signature, 204, 400, 401, event filtering |
| `apps/rebilling/tests/test_reconciliation.py` | list and paging, backfill, errors, command |
| `apps/rebilling/tests/test_tasks.py` | Retell Celery task, beat schedule |
| `apps/rebilling/tests/test_twilio.py` | Twilio pull, paging, update vs insert, per-client failure, command, Twilio task, schedule |
| `apps/rebilling/tests/test_billing.py` | bill calculation, rate-in-effect billing, mid-month rate splits, integer cents, command |
| `apps/rebilling/tests/test_admin_mapping.py` | client mapping serializer validation, set_mapping service, AdminClientMappingView auth and errors |
| `apps/rebilling/tests/test_stripe_billing.py` | Stripe customer lookup, invoice creation, idempotency, webhook signature verification, event handling |
| `frontend/src/features/admin/AdminPage.test.tsx` | SSO handshake, live client list, detail fetch, mapping edit/setup flows, validation |
| `frontend/src/features/admin/AdminPreviewPage.test.tsx` | demo rendering, period filtering, unmapped client setup, in-memory mapping edits |

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
- **A failed nightly run only logs.** There is no alert until Phase 7. Until then, a silently
  failing Retell audit or Twilio pull under-bills with no visible signal.
- **Django admin** now has `ClientMapping` registered, but GHL models have no admin. The GHL tokens
  and OAuth history cannot be inspected through the admin yet.
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
- **The admin page mapping UI is built and tested**, but not verified in a live GHL iframe yet.
  SSO handshake, all backend endpoints, and frontend component rendering are tested; GHL iframe
  embedding and the live mapping save workflow have not been verified inside GHL.
- **`billing_email` is unpopulated by the UI.** Every client needs one filled in by hand before
  Phase 5 charging will work. Phase 4's mapping editor (not yet built) should include this field.
- **The Stripe webhook is a single point of failure.** If it is unregistered or the signing secret
  is wrong, every charge still succeeds at Stripe while every bill here reads `charging` forever.
  No reconciliation pass exists; Phase 6 should alert on bills stuck in `charging` for N days.
- **Stripe has a $0.50 minimum charge.** A bill under 50 cents will fail at the card. No policy
  has been decided (roll forward, waive, or let it fail).

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

## 17. Recent session: Client mapping integration (Task 4 Phase 4 mapping UI)

Labelled "Phase 3" at the time this was built; reconciled against plan.md's own numbering in §18 —
the mapping UI is Phase 4's write half, built ahead of Phase 3's calculation engine.

**Task:** Integrate the manual mapping feature from the `maping` branch into current `main`,
preserving live/preview route separation.

**Backend changes:**
- `ClientMappingSerializer` (new): validates Retell agent IDs (non-blank, deduplicated), phone
  numbers (7–15 digits, deduplicated, normalized), and Twilio subaccount SID (regex match,
  blank allowed). Replicates serializer validation logic on each save (not relying on model
  defaults).
- `set_mapping()` service (new): creates or updates a `ClientMapping`, writing only the fields
  sent (leaves others untouched on partial updates).
- `AdminClientMappingView` (new): `PUT /api/rebilling/admin/clients/<location_id>/mapping/`. Agency
  scope only. Returns the updated mapping or raises 403 (sub-account), 404 (unmapped/unonboarded).
- `ClientMapping` admin registration: displays `client_name`, `location_id`, `active`, `billing_mode`,
  `twilio_subaccount_sid`; filters by `active`, `billing_mode`; searches by `client_name`,
  `location_id`.
- `test_admin_mapping.py` (new): 12 tests covering valid saves, partial updates, deduplication,
  validation rejections, auth failures, 404 on unonboarded location.

**Frontend changes:**
- `MappingEditor.tsx` (new): unified editor component for Retell agents, phone numbers, and Twilio
  SID. Chip-list UI for list fields. Two modes: live (calls `saveMapping` API) and preview (writes
  to in-memory store, no network). User-friendly error messages.
- `ClientDetail.tsx`: conditional mapping editing. `canEdit` prop determines whether to render
  `MappingEditor` (editable) or read-only `<dl>` (not editable).
- `ClientBrowser.tsx`: new `SetUpMapping` component for unmapped clients (agency admins only).
  Calls `onMappingSaved` callback on successful save, triggering a re-fetch of both detail and
  list (to mark the client as mapped).
- `ClientsTable.tsx`: unmapped rows clickable for agency admins (non-clickable for sub-accounts).
- `AdminPage.tsx` (live): added 404-avoidance optimization. Skips detail fetch when clients-list
  summary already marks the row `mapped: false`, synthesizing the unmapped shape directly from the
  summary. New `handleMappingSaved()` callback re-fetches both detail and list.
- `AdminPreviewPage.tsx`: added dummy `bumpPreview` state trigger to force re-renders after in-memory
  edits. Passed `preview={true}` and `onMappingSaved` to `ClientBrowser`.
- `demoData.ts`: added module-level `previewMappings` map (`savePreviewMapping`, `resetPreviewMappings`).
  `withPreviewMapping()` applies edits to demo specs. `unmappedOrSetUp()` returns mapped summary if
  an edit exists. All functions read the map, so demo edits persist across navigations with zero API
  calls.
- `admin.css`: added full "mapping editor" block (`.mapping-editor`, `.mapping-field`, `.chip-list`,
  `.chip`, `.chip-add`, styling for inputs and buttons).
- `AdminPage.test.tsx`: 3 new tests (live edit, setup unmapped client, validation error).
- `AdminPreviewPage.test.tsx`: replaced 1 old test ("unmapped not clickable") with 3 new tests
  (open unmapped to setup, edit mapping and persist, setup marks client as mapped). Added
  `afterEach(() => resetPreviewMappings())` to prevent test bleed.

**Documentation:** `CLAUDE.md` updated with five edits: mapping endpoint description, serializer
validation, `set_mapping()` service, Django admin registration, in-memory preview store, 404-avoidance
pattern.

**Tests:** 176 backend passing (↑12), 43 frontend passing (↑5 net, including 1 replaced), linting
clean, `tsc`/`vite build` clean.

**Status:** All acceptance criteria met. Changes staged/unstaged, not committed (per task constraint).

---

## 18. Recent session: voice rebilling Phase 3 (calculation engine)

**Task:** Build Phase 3 of Task 4 — the billing calculation engine — per `plan.md`'s "Phase 3 —
calculation" section: `Bill`/`BillLine` models and migration, the calculation service and
management command, adherence to the financial rules, and tests.

**Models** (`apps/rebilling/models.py`, migration `0004`): `Bill` (`client`, `billing_period`
`YYYY-MM`, `total_cents`; `unique_together(client, billing_period)`), `BillLine` (`bill`,
`line_type` — `retell`/`twilio`, `quantity_minutes`, `cost_cents`, `bill_cents`). `RetellCall.bill`
and `TwilioUsageDay.bill` added, both nullable and `on_delete=SET_NULL` — deleting a bill unlinks
those rows but never deletes the underlying call or usage-day data. No `created_at` or other
audit field was added to `Bill`; nothing in the plan or the calculation logic needed one.

**Service** (`apps/rebilling/services.py`): `calculate_bills(month=None)` plus
`_bill_one_client`, `_retell_line`, `_twilio_line`, `_rate_in_effect` — plain functions, matching
the existing style (`store_call`, `match_client`, `usage_summary`) rather than a new service class.
Reuses `month_window()` for period parsing (same validation the admin endpoints already use) and
`_calls_in_window()` for the Retell date-range query, rather than duplicating either. Full details
and the design reasoning are in §5.8 above.

**Command:** `python manage.py calculate_bills [--month YYYY-MM]`, modelled on the existing
`pull_twilio_usage`/`reconcile_retell_calls` commands' terse style.

**Tests:** 18 new tests in `test_billing.py`, plus one more (19 total) added when `bill`'s
`on_delete` was changed to `SET_NULL`, verifying a deleted bill unlinks rather than deletes the
underlying `RetellCall`/`TwilioUsageDay` rows.

**Bugs caught and fixed before this was reported done:** an early draft called a rate-lookup
function that didn't exist, duplicated `_calls_in_window()`'s datetime-window logic, truncated
cents with `int()` instead of the codebase's `ROUND_HALF_UP` convention, and added a `created_at`
field nobody asked for. All four were caught and fixed in review before the tests were considered
final, and a `created_at`-less migration was the one actually applied.

**Phase-numbering correction (this session):** `plan.md`'s own phase numbers — Phase 3 is the
calculation engine, Phase 4 is the admin page (read-only viewing, brought forward, plus the
write/mapping/rates/bills half) — are authoritative. §5, §5.8/5.9, §6's title, §7's title, and §17
above previously mislabeled the already-built mapping-edit UI as "Phase 3"; they now say Phase 4,
and Phase 3 is recorded as built where it actually is.

**Deliberately not built:** the plan also calls for running Twilio's month-close pull before
calculating, so the bill is built on settled days; `calculate_bills` does not trigger that pull
itself (see §5.8) — it stays two separate commands run in order. No admin/API exposure of bills
(Phase 4) and no charging (Phase 5) were built either.

**Status:** Backend 195 tests passing (176 + 19), lint clean, `makemigrations --check` clean,
`manage.py check` clean. Not committed (no commit was requested).

---

## 19. Recent session: Stripe billing (Phase 5 charging)

**Task:** Build Phase 5 of Task 4 — the Stripe billing module — for automatic off-session charging
of calculated bills. Replaces the earlier GHL vs Stripe research phase with a Stripe implementation:
customer lookup, invoice creation with idempotency, webhook handlers, and tests.

**Why Stripe:** Research determined that GHL does not process payments (it is a control plane over a
connected provider); choosing Stripe means choosing the payment rail directly. Phase 4 remains
unchanged; it will work with either.

**Models** (`apps/rebilling/models.py`, migration `0005`):
- `ClientMapping` gains `billing_email` (new field, required for Stripe customer lookup) and
  `stripe_customer_id` (cached after first lookup).
- `Bill` gains `status` (choices: `uncharged`, `charging`, `paid`, `failed`), `stripe_invoice_id`,
  and `paid_at` (Stripe's own timestamp from webhook, not server time).

**Module** (`apps/rebilling/stripe_billing.py`, new):
- `customer_id(client)` — looks up or creates a Stripe customer by `billing_email`. A client who
  already pays through this account is found and reused (no duplicate cardless customers).
  Cached on the first lookup.
- `setup_link(client)` — returns a Stripe Checkout Session URL (`mode='setup'`) for clients
  needing to attach a card before auto-charge works.
- `charge_bills(month=None)` — the main function. Picks up `uncharged` bills for the month,
  skips non-auto-charge clients, charges each via Stripe, and returns a result dict with counts.
- `_charge_one(bill)` — internal. Creates a Stripe invoice (`collection_method='charge_automatically'`,
  `auto_advance=True`), saves the invoice ID immediately (for traceability on failure), adds
  line items from `BillLine` rows (amounts in integer cents), finalizes it (triggers card attempt),
  and sets bill status to `charging`.
- `read_event(payload, signature)` — verifies webhook signature using `stripe.Webhook.construct_event`.
- `apply_event(event)` — processes `invoice.paid` and `invoice.payment_failed` events, updating
  bill status and `paid_at`.

**Idempotency:** Every Stripe call carries an idempotency key derived from `Bill.pk`
(`bill-{pk}-invoice`, `-{line_type}`, `-finalize`). A re-run of the charge command re-reaches the
same Stripe objects instead of creating duplicates. Combined with a DB filter for `uncharged` bills
only, double-charging is impossible even after a crash.

**Webhook** (`POST /api/rebilling/webhooks/stripe/`, new):
- Reads raw request body before parsing (signature covers bytes, not parsed JSON).
- Verifies `Stripe-Signature` header. Returns 401 on failure, 400 on invalid JSON.
- Calls `apply_event()` on valid events. Returns 204 (success or event type ignored).

**Management command** (`python manage.py charge_bills [--month YYYY-MM]`, new):
- Charges all `uncharged` bills for a month (defaults to this month).
- Skips clients with `billing_mode != auto_charge` (respects "send invoice" mode).
- Logs per-client failures but continues for others (one failure doesn't stop the run).
- Prints count of charged bills and total cents.

**Settings:**
- `STRIPE_SECRET_KEY` — API secret for this account.
- `STRIPE_WEBHOOK_SECRET` — webhook signing secret (different from the API key).

**Tests** (26 new, in `apps/rebilling/tests/test_stripe_billing.py`):
- Customer lookup: cached on second call, created when not found, duplicates avoided (found by
  email first).
- Invoice creation and finalization: lines added with integer cent amounts (type checked), invoice
  is finalized which starts the card attempt.
- Idempotency: keys are derived correctly, re-running the month charges nothing twice.
- Skipping: non-auto-charge clients are skipped and counted, other clients still charged.
- Failure isolation: one client's failure doesn't stop the others.
- Webhook: signature verification (401 on wrong secret), event routing (paid/failed handled, others
  ignored), state updates (bill status and `paid_at` set correctly).
- No Stripe SDK calls reach the network; a `FakeStripe` class with record-and-return methods
  mocks the SDK, and webhook signatures are built by hand.

**Risks documented in `CriticalFindings.md` §2.6 (new):**
- **$0.50 minimum charge** — a bill under 50 cents fails at the card. No policy yet (roll forward,
  waive, or fail).
- **`billing_email` is new and unpopulated** — no UI fills it in yet; every client needs one by hand.
- **Webhook is the single point of failure** — no reconciliation against Stripe. A bill can stay
  `charging` forever if the endpoint is unregistered or the secret is wrong.
- **`failed` is not final** — Stripe's Smart Retries can flip a failed invoice to `paid` later.

**Not built:** automatic scheduling in Celery Beat (would be 4 lines; say the word).

**Status:** 221 backend tests passing (195 + 26 new), lint clean, `makemigrations --check` clean,
`manage.py check` clean. Not committed (per standing rule).

---

## 20. Git history

| Commit | Message (abridged) |
|---|---|
| `da0ddb3` | GHL Onboarding |
| `9e7f703` | Minor update, adding location id in .env, and removing company id |
| `6d0b7b1` | feat(rebilling): implement Phase 1 models, Retell webhook, and call matching |
| `44c3d94` | feat(rebilling): implement Phase 2 Retell nightly reconciliation audit |
| `d9ab56a` | feat(rebilling): add unbillable call tracking and Celery daily audit schedule |
| `ea95a0c` | Integrated twilio calls with Retell. This is the Phase 2b work |
| `1d9d280` | feat: Agency Admin Custom Page with white and green theme and demo seed data |
| `4012b52` | feat(admin): resolve agency scope, handle unmapped clients gracefully, and separate demo preview |
| `f0ec6cc` | fix(ghl): use version v3 for installed-locations and decouple initial sync |
| `cc8e982` | fix(ghl): sync all agency sub-accounts regardless of app install status |
| `3e383bb` | feat: add refresh clients button and location-created webhook |
| `4b21240` | feat(rebilling): integrate client mapping editor for live and preview routes |

The working tree has **uncommitted changes** for **Task 4 Phase 5 Stripe charging** (§19):
- Backend: `models.py` (+billing_email, +stripe_customer_id on ClientMapping; +status/stripe_invoice_id/paid_at on Bill),
  `exceptions.py` (+StripeBillingError), `stripe_billing.py` (new module), `views.py` (StripeWebhookView),
  `urls.py` (+webhook endpoint), `settings/base.py` (+STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET),
  `settings/test.py` (test values for both), `migrations/0005_...py` (new), `management/commands/charge_bills.py` (new),
  `tests/test_stripe_billing.py` (new, 26 tests), `requirements/base.in` (+stripe), and all three `.txt` recompiled
- Dependencies: `stripe==16.0.1`
- Documentation: `CLAUDE.md` and `plan.md` + `CriticalFindings.md` (local, gitignored)

---

## 21. Memory and collaboration

- Claude's persistent memory for this project is indexed by `MEMORY.md` and currently holds one
  entry: **avoid over-engineering**. Build exactly what is asked, with no speculative extras.
- The user pastes multi-part specs and expects each part to be worked through in full, with
  anything not done called out explicitly.
- The user wants short, direct summaries: what changed, what was verified, and what was deliberately
  left out.

---

## 22. Recent session: Task 4 Phase 5 dual-rail charging (GHL invoice + Stripe fallback)

**Task:** build the full dual-rail charge on `feat/ghl-billing-with-stripe-fallback`: merge the
Stripe engine in, add the GHL invoice rail in front of it, and a 72-hour fallback between them.
The wallet path is formally closed (§21).

**The merge.** `feat/stripe-billing` merged in with one conflict, `views.py`, where both branches
had appended imports — both sets kept. Commit `d3f7201`. Its migration `0005` came with it, so the
Stripe fields (`Bill.status`, `stripe_invoice_id`, `paid_at`, `ClientMapping.billing_email`) are
not new work here.

**What was built:**

- **`apps/rebilling/ghl_billing.py`** — `GHLInvoiceService.create_invoice` / `send_invoice` /
  `void_invoice`, plus `apply_event` for the webhook. Invoices are raised in the new
  **`GHL_MAIN_LOCATION_ID`**, never `AGENCY_LOCATION_ID`; a blank setting refuses to charge rather
  than invoicing an account that holds no contacts. `send_invoice` sends
  `autoPayment: {enable: true}` and nothing else, which is the whole mechanism — no public
  endpoint reads a saved card, so there is no `paymentMethodId` to send.
- **`apps/rebilling/dual_rail.py`** — `charge_bills(month)`, now the only thing that may start a
  charge. GHL first, then a sweep of `charging` bills older than `BILLING_FALLBACK_WINDOW_HOURS`
  **across every month**, not only the one being charged.
- **`POST /api/rebilling/webhooks/ghl-invoices/`** — `InvoicePaid` only. The Ed25519 verification
  moved out of `apps/ghl/views.py` into **`apps/ghl/webhooks.py`** so both GHL webhooks share one
  check instead of two copies.
- **Migration `0006`** — `Bill.ghl_invoice_id`, `ghl_invoice_voided_at`, `charged_at`.
- **`stripe_billing._charge_one` → `charge_on_stripe`**, now that the fallback is a second caller.

**Three decisions worth knowing:**

- **A `failed_ghl` status was drafted and then removed.** GHL publishes no charge-failed webhook,
  so nothing could ever have written it. A decline and a silent no-op both arrive as a `charging`
  bill that never becomes `paid`; age is the only signal either gives, which is why the fallback
  is a timer and not a reaction. `Bill.status` stayed at four states, and which rail a `charging`
  bill is on is read off the two invoice ids.
- **The void is a gate, not a step.** A GHL invoice that will not void stops the fallback entirely
  (counted as `fallback_blocked`) and waits for a human. A bill nobody charged can be charged
  tomorrow; a bill charged on both rails has taken the money twice.
- **`items[].amount` is sent in dollars**, from the `number`-not-`integer` inference — still
  unverified. The division is in `ghl_billing._item()` and nowhere else, so a 100× error has one
  site to fix.

**Contact details and billing email** are read from GHL per invoice rather than stored (two open
questions closed in `CriticalFindings.md` §5). The email is written back to
`ClientMapping.billing_email`, which is the only thing carrying across to the Stripe rail.

**Tests:** 28 new in `tests/test_dual_rail.py` — both rails, the window boundary either side, the
void gate, no-double-fallback, the cross-month sweep, and the webhook including four GHL events
that must change nothing. Two command tests moved out of `test_stripe_billing.py`, since the
command is dual-rail now. **257 backend, 50 frontend, lint/format/migrations/`check` all clean.**

**Left undone, deliberately:** nothing is on the Celery schedule (the command is manual, as the
Stripe path already was); no Phase 6 alerting; the admin page still shows Phase 3's estimate
rather than these bills.

**`stripe_billing.charge_bills()` kept as a gated escape hatch (follow-up, same session):** the
user asked to keep it rather than delete it, on the condition that it can never fire except by
strict manual choice. `manage.py charge_bills` now takes `--stripe-only`, which requires a second
flag, `--i-understand-this-skips-ghl`, passed in the same invocation — neither does anything
alone, so a typo, a copied command, or a cron job inheriting old arguments cannot trigger it.
Nothing on the Celery schedule ever passes either flag; the schedule doesn't call this command at
all. The escape hatch only ever touches `status=uncharged` bills, the same filter the GHL rail
uses, so it structurally cannot double-charge a bill GHL already has a live invoice out for — that
bill is already `charging` and neither path looks at it. Five new tests in `test_dual_rail.py`
cover: the default command never reaching the hatch, `--stripe-only` alone being rejected,
`--i-understand-this-skips-ghl` alone doing nothing, both flags together charging through Stripe
with no GHL call even registered, and the confirmation message. **262 backend tests passing.**

**Still blocked on one live test:** whether `autoPayment: {enable: true}` actually debits the
card (§21). The code does not change whichever way it goes — "sent forever" is exactly what the
72-hour sweep handles — but until it is run, "cards are charged automatically" is an assumption
on the GHL rail, not a fact.

---

## 23. Recent session: GHL location token handling for live test-phase topology (30 Oct)

**Context:** Phase 5 dual-rail charging (§22) requires invoices to be raised in `GHL_MAIN_LOCATION_ID`
(the sub-account holding client contacts and saved cards). Jerry's test account intentionally has
`GHL_MAIN_LOCATION_ID` equal to `AGENCY_LOCATION_ID` (`RKzecZGFmWPnQfpEZTr5`), where the test
contact and card are verified to live. GHL's `/invoices/` and `/contacts/` endpoints require
`authClass: Location` tokens (not Company tokens), but `sync_locations()` deliberately never creates
a location-scoped row for `AGENCY_LOCATION_ID` (it excludes this id so the agency never shows up as
a fake client sub-account).

**The solution:** `GHLOAuthService.get_location_token()` now special-cases `AGENCY_LOCATION_ID` via
a new `_agency_location_token()` method:
- **First call:** mints a fresh `authClass: Location` token from the agency token (GHL accepts this
  id as a valid `locationId` for `/oauth/locationToken`), stores it in a new `is_agency=False` row,
  and returns the token.
- **Refresh needed:** re-mints a fresh token.
- **After sync_locations():** `sync_locations()` deactivates the row (since this id is excluded from
  the installed/client set). On the next `get_location_token()` call, the row's `is_active=False`
  status triggers a remint, so the token is always fresh and valid for billing calls.

The row is otherwise identical to one `sync_locations()` would have created for a real sub-account —
this handles the self-healing needed for the test topology while staying self-contained and not
disrupting the broader architecture.

**GHL invoice payload:** The GHL Invoices API v3 requires a top-level `businessDetails` object with
at least a `name` field. This is set to `GHL_BUSINESS_NAME` (env var, defaults to `'SurPro Labs AI'`).
The payload construction is in `ghl_billing.GHLInvoiceService.create_invoice()`.

**Tests:** Three new in `apps/ghl/tests/test_services.py` (mint on first call, reuse when fresh,
remint after sync deactivates). Two tests in `apps/rebilling/tests/test_dual_rail.py` verify the
full flow works. Removed a guard test that was checking for an error that no longer happens.

**No hard guard:** The earlier approach added a hard guard rejecting `GHL_MAIN_LOCATION_ID == AGENCY_LOCATION_ID`.
That was removed because Jerry's real test account uses this exact configuration. The guide in
`local_folder/plan.md` ("Live test-phase topology") documents why the two ids are the same for now,
and that production is still expected to move to a separate Main Sub-Account eventually. Nothing
enforces the separation in code; it was a deliberate choice to avoid blocking the test.

**Verified:** 265 backend tests passing (257 + 8 new: 3 in services, 2 in dual_rail, 3 removed/changed).
Lint, format, and migrations all clean.

**Status:** All changes ready to push to the server. The end-to-end `python manage.py charge_bills`
test can now run with Jerry's test account and contact.

---

## 24. How to start a new session on this project

1. Read this file, then `CLAUDE.md`.
2. If the task touches rebilling, read `local_folder/plan.md` and `local_folder/CriticalFindings.md`.
3. Confirm the baseline. From `backend/`, run `.venv/Scripts/python.exe -m pytest -q` and expect
   **265 passed** (backend). From `frontend/`, run `npm test -- --run` and expect **50 passed**
   (frontend). Also run `ruff check .` and `manage.py makemigrations --check --dry-run`.
4. Check `git status` and `git log --oneline | head` before changing anything.
5. **Note:** Task 4 Phases 1, 2, 2b, 3, 4 (mapping edit UI), and 5 (dual-rail charging) are
   built — see §§22–23. Phase 4's rate editing, unassigned-call mapping, and bills display in the
   admin page remain. Phase 6 (alerts) is not started. Tasks 2 and 3 are not started. Don't start
   any of these without an explicit request. Ask first if the request is ambiguous.
   **Never make live API calls** — and note Phase 5 is the first code here that can move real
   money, so `GHL_INVOICE_LIVE_MODE` stays false until Jerry says otherwise.
