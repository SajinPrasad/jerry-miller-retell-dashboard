# Handover — SurPro Labs AI

Read this first. It explains what the project is, how it is built, what is done, what is not,
and what must not happen. It is written so a new session, human or agent, can pick the work up
cold. Secrets, server addresses, real client account ids and personal contact details are
deliberately absent: they live in `backend/.env` and on the server. Keep it that way when editing.

Companion docs:

- `CLAUDE.md`: project instructions, structure, commands and engineering rules. Authoritative
  for how to work here.
- `local_folder/plan.md`: the phased plan for Task 4 (voice rebilling). Gitignored; exists only
  on the development machine.
- `local_folder/CriticalFindings.md`: contradictions between the spec PDFs and the real GoHighLevel,
  Retell and Twilio APIs, the financial safeguards, and the open questions. Gitignored.
- `local_folder/pdfs/`: the source PDFs. Gitignored.

---

## 0. Current state (read this first)

**Branch.** `feat/retell-workspace-per-subaccount`, off `main` at `867f963`. Do not commit or push
unless asked.

> ### Phase 9 changed the Retell half of this document (4 Nov)
>
> **There is one Retell workspace per GHL sub-account now, and no account-level key.** Each client
> has their own encrypted API key on `RetellWorkspace`, their own webhook URL
> (`/api/rebilling/webhooks/retell/<location_id>/`), and their own pass in the nightly audit.
> **`RETELL_API_KEY` no longer exists**, nor does the account-level webhook route,
> `match_client()`, `store_call()` or `UnassignedCall` (dropped in migration `0010`).
>
> Sections **6.4**, **6.7**, **15** and **17** below still describe the single-account design in
> places. `local_folder/plan.md` **Phase 9** is the authoritative description of what is built;
> `CriticalFindings.md` §2.8 and R6–R10 carry the risks that came with it. The single sharpest:
> **Retell signs a workspace's webhooks with one specific key** — the one carrying the webhook
> badge — so any other key of that workspace tests green here and then fails every signature,
> silently, per client.

**Uncommitted work (Phase 8, the rebilling status write — §6.10b):** `sync_rebilling_status` now
writes `failed` / `active` to a custom field on the client's **billing contact in the main
account**, not to the `retell_rebilling_status` custom value in their own sub-account.

- **Why:** GoHighLevel workflows have no trigger for a custom value changing. The status was
  written faithfully every month into a field nothing could watch, so Jerry's pause/resume
  workflow never ran. A contact custom field is what "Contact Changed" fires on.
- It reuses Phase 7's `set_contact_field()`, so there is one way a client's status reaches GHL.
- `GHL_RETELL_STATUS_CUSTOM_VALUE_NAME` is replaced by `GHL_RETELL_STATUS_CUSTOM_FIELD_ID`, blank
  until Jerry creates the field. `GHLOAuthService.set_custom_value()` and the
  `locations/customValues.*` scopes are gone — nothing reads or writes a custom value any more.
- A client with no `billing_email` has no contact to write to: logged and skipped, never guessed.
- Still best effort — a GHL failure is logged, and the bill's own outcome stands.
- Files: `backend/apps/ghl/services.py`, `backend/apps/rebilling/stripe_billing.py`,
  `backend/config/settings/{base,test}.py`, `backend/.env.example`,
  `backend/apps/rebilling/tests/{conftest,test_retries}.py`, `backend/apps/ghl/tests/test_services.py`,
  `CLAUDE.md`, `handover.md`, `local_folder/plan.md`. No migration, no frontend change.

**Already committed:** Phase 7, the card on file (`a14e0a3`, §6.11) — the live Stripe card check on
every detail view, the **Request card** button, and the webhook back. Before it, the automated retry
schedule and the first GHL rebilling-status write (`bcd48b9`, the custom value Phase 8 replaces),
and the Twilio billing pause (`654c927`) — Twilio minutes are no longer billed or shown anywhere,
behind `TWILIO_BILLING_ENABLED`.

**Test status (last run, this state):**

- Backend: `pytest`: **355 passed** (four custom-value tests gone, one new one for a client with no billing email).
- Frontend: `npm test`: **96 passed** in 3 files.
- `npx tsc -b` (in `frontend/`): exit 0. `npx oxlint src`: clean.
- `ruff check .` and `ruff format --check .`: clean. `manage.py check`: clean.
- `manage.py makemigrations --check --dry-run`: "No changes detected".

**Nothing in Phase 7 or 8 has run against live GoHighLevel or Stripe.** Every call is mocked. The
contact search and update shapes come from GHL's published v3 spec, not from a live call — §17 has
what to check first. Both phases now wait on the same thing: the two field ids, and a
re-authorisation for `contacts.write` (§17.1a, step 2). The sidebar iframe also needs an SSO
bridge injected into GHL's Agency Custom JavaScript (§17.1a, the bridge script).

**Not started:** Task 2 (agency dashboard), Task 3 (client dashboard), Phase 6 of Task 4 (alerts),
and the parts of Phase 4 still open: a UI for mapping unassigned calls, and a bills table.
The most important open question is in §15: which Stripe account the charges go through.

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

**Twilio voice billing is paused** (`TWILIO_BILLING_ENABLED`, default off). GoHighLevel rebills
regular call minutes natively for these sub-accounts, so billing them here too would charge the
client twice. The daily pull still runs and `TwilioUsageDay` rows are still stored, so turning the
setting back on resumes billing without a backfill. While it is off, no bill line, invoice or
charge can carry Twilio usage, and the admin page hides every Twilio field. **The product is
Retell AI minutes only.**

### 1.1 Terms used in the code and this file

| Term | Meaning |
|---|---|
| Agency | Jerry's GHL agency account. Its token is the one that mints sub-account tokens. |
| Sub-account / location | One client's GHL account. `location_id` identifies it. |
| Agency scope / location scope | What the embedded admin page's viewer may see. Agency sees every client; a sub-account sees only itself. |
| Mapping (`ClientMapping`) | One client's link to its Retell agents, backup phone numbers and Twilio subaccount. |
| AI minutes | Minutes of Retell voice-agent calls. Billed at `voice_rate_cents` (default 25¢ a minute). |
| Call minutes | Twilio regular call minutes (the `calls` category). Rated at `call_rate_cents` (default 5¢ a minute), but **not billed while `TWILIO_BILLING_ENABLED` is off**. |
| Billable / unbillable call | A stored Retell call that can / cannot be billed, with a plain-English reason when it cannot. |
| Estimate | `minutes × current rate`, shown on the page until the month has a bill. Never a bill. |
| Bill (`Bill`, `BillLine`) | One client's total for one month, with a line per source. Created by `calculate_bills`. |
| Bill status | `uncharged` → `charging` → `paid`, or `failed`. See §6.10. |
| Rate history (`RateHistory`) | Rates per client from a start date. A change adds a row; old months keep their old rate. |

---

## 2. The four tasks and where each stands

| # | Task | Due | Status |
|---|---|---|---|
| 1 | Marketplace app: GHL OAuth install, agency token, per-sub-account tokens | Oct 23 | **Built** (backend and onboarding page) |
| 1b | Agency admin page: client list, usage, rates, mapping, bill status | — | **Built.** `/admin` is live (GHL SSO). `/admin/preview` is sample data and never calls the API |
| 2 | Agency dashboard: MRR, live clients, leads by source, clients at risk, voice revenue | Oct 30 | **Not started** (`apps/dashboards` is an empty scaffold) |
| 3 | Client dashboard: one client's leads, booked jobs, response time, voice usage | Nov 4 | **Not started** (the `client-dashboard` feature folder is empty) |
| 4 | Voice rebilling: Retell + Twilio voice, split per client | Nov 13 | **Phases 1, 2, 2b, 3, 5 built.** Phase 4 mostly built (mapping, rates, per-call and bill status). Still open: mapping of unassigned calls and a bills table. Phase 6 (alerts) not started |

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
      ghl/                  Task 1: GHL OAuth onboarding, sub-account sync, SSO session, location webhook
      rebilling/            Task 4: Retell + Twilio voice rebilling, bills, Stripe charging, admin API
        models.py  services.py  stripe_billing.py  serializers.py  views.py  urls.py
        management/commands/  calculate_bills, charge_bills, pull_twilio_usage, reconcile_retell_calls
        migrations/           0001 to 0006 (see §6.2)
        tests/                one module per concern, plus conftest.py (shared guards)
      dashboards/           Tasks 2-3: EMPTY scaffold
    requirements/           *.in (edit) -> *.txt (pip-compile output, committed)
    .env                    secrets. Gitignored. Never commit, never print
    .env.example            every setting, documented
    ruff.toml, pytest.ini
  frontend/                 React 19 + TypeScript + Vite
    src/App.tsx             routes: /onboarding, /admin, /admin/preview
    src/api/                admin.ts, client.ts (fetch wrapper), ghl.ts
    src/lib/ghlSession.ts   the GHL iframe handshake that yields the SSO blob
    src/types/              admin.ts, ghl.ts
    src/features/onboarding/  built (GHL connect page)
    src/features/admin/       built (live and preview admin pages, mapping and rates editors)
    src/features/agency-dashboard/, client-dashboard/  EMPTY
  local_folder/             gitignored: PDFs, plan.md, CriticalFindings.md
```

### 3.2 Stack

- **Backend:** Python 3.12, Django 5.2, Django REST Framework, django-filter, django-environ,
  httpx (all outbound HTTP), cryptography (Fernet), celery[redis] with a Redis broker, whitenoise,
  gunicorn, psycopg (Postgres planned; SQLite locally and in the current deployment), retell-sdk
  (only `retell.lib.verify` is used), stripe 16.0.1.
- **Dev and test:** pytest with pytest-django, respx 0.23.1 (mocks httpx), pytest-cov, ruff.
- **Frontend:** React 19, TypeScript, Vite (dev server proxies `/api` to `:8000`),
  react-router-dom 7, vitest with Testing Library, oxlint.
- **Dependency rule:** change dependencies only in `requirements/*.in`, then `pip-compile`, then
  `pip-sync`. Never `pip install` a project dependency directly.

### 3.3 Settings

`config/settings/base.py` holds everything. `development.py`, `production.py` (secure cookies,
HSTS, and `X_FRAME_OPTIONS = 'ALLOWALL'` because the app is embedded in GHL) and `test.py`
(in-memory SQLite, fast password hashing, fixed fake secrets) inherit from it. `manage.py` defaults
to `config.settings.development`. pytest uses `config.settings.test` (see `pytest.ini`).

### 3.4 URL map

All under `/api/` unless noted.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/oauth/callback/` | GHL redirects here after install. Exchanges the code, syncs sub-accounts, redirects to the frontend |
| GET | `/api/ghl/onboarding/` | Install link, agency, sub-accounts. **Never returns tokens** |
| POST | `/api/ghl/onboarding/sync/` | Re-syncs sub-accounts from GHL |
| POST | `/api/ghl/sso/session/` | Opens the SSO payload the embedded page got from GHL: who is viewing, and at what scope |
| POST | `/api/ghl/webhooks/location-created/` | GHL's "location created" event. Syncs the new sub-account |
| POST | `/api/rebilling/webhooks/retell/` | Retell `call_ended` webhook. Signature-verified, answers 204 |
| POST | `/api/rebilling/webhooks/stripe/` | Stripe invoice events (`invoice.paid`, `invoice.payment_failed`). Signature-verified, answers 204 |
| GET | `/api/rebilling/admin/clients/?month=` | Every sub-account, left-joined to its mapping, with that month's usage and status |
| POST | `/api/rebilling/admin/clients/sync/?month=` | Agency only. Re-syncs sub-accounts from GHL, then returns the list |
| GET | `/api/rebilling/admin/clients/<location_id>/?month=` | One client: mapping, rates, bill, usage, Twilio days, recent calls |
| PUT | `/api/rebilling/admin/clients/<location_id>/mapping/` | Agency only. Sets Retell agents, backup phone numbers, Twilio subaccount |
| PUT | `/api/rebilling/admin/clients/<location_id>/rates/?month=` | Agency only. Saves the client's two rates from today, returns the refreshed detail |
| GET | `/api/rebilling/admin/retell-agents/` | Agency only. Every agent on Jerry's Retell account, for the picker. 502 if Retell fails |
| GET | `/api/rebilling/admin/twilio-subaccounts/` | Agency only. Every subaccount under Jerry's Twilio parent. 502 if Twilio fails |
| — | `/admin/` | Django admin. `ClientMapping` and `Bill` are the registered rebilling models; GHL models have no admin registration |

Frontend routes: `/onboarding` (GHL connect page), `/admin` (live admin page, needs GHL SSO),
`/admin/preview` (sample data, development only). Anything else redirects to `/onboarding`.

---

## 4. Task 1: GHL onboarding (built)

**Flow:** the agency admin opens `/onboarding` (React) → "Connect to GoHighLevel" → GHL install
screen → GHL redirects to `GET /api/oauth/callback/?code=…` → the backend exchanges the code for
the **agency token** (`user_type=Company`) → rejects the install if it was made on a sub-account →
stores the agency row → syncs sub-accounts → redirects to
`FRONTEND_URL/onboarding?status=connected` (or `?error=install_cancelled` / `?error=install_failed`).
**Sync failures do not fail the install**: the agency connection counts as successful once the code
is exchanged; sync logs a warning and continues.

**Model:** `GHLOAuthToken` (`apps/ghl/models.py`). One table holds both agency and sub-account
tokens. Fields: `company_id`, `location_id` (blank on the agency row), `name`, `is_agency`,
`is_active`, `access_token`, `refresh_token` (agency row only), `expires_at`, and, from migration
`0002`, the sub-account owner's `owner_first_name`, `owner_last_name` and `owner_email` (read-only,
from GHL). Tokens are Fernet-encrypted with `FIELD_ENCRYPTION_KEY`. A sub-account row can have an
empty `access_token` when the app isn't yet installed on it; it still appears in the admin page.

**Service:** `GHLOAuthService` (`apps/ghl/services.py`):

- `install_url()` builds the chooselocation URL on `GHL_INSTALL_URL_BASE`.
- `exchange_code(code)` stores the agency token. `company_id` comes **from the token response**,
  not from config.
- `get_agency_token()` refreshes the token when it is within 5 minutes of expiry. **Refresh tokens
  are single-use**, so it takes a `select_for_update` lock first.
- `get_location_token(location_id)` re-mints a sub-account token from the agency token when it is
  expiring.
- `sync_locations(agency)` calls `GET /oauth/installed-locations` with the `Version: v3` header
  (the default `2021-07-28` returns 404 for this endpoint), then `POST /oauth/locationToken` for each
  sub-account. It skips `AGENCY_LOCATION_ID`. If a sub-account's token can't be minted, it stores the
  row anyway with an empty `access_token` and a future `expires_at` (so it appears in the admin page),
  logs GHL's reason, and continues. It also copies each owner's name and email onto the row.
- **Error handling in `_request()`:** catches `httpx.HTTPStatusError` separately and includes the
  response body (capped at 500 characters) in the exception, so GHL's reasons are visible in logs.
- `sync_billing_emails()` (in `rebilling`) copies each owner's email onto `ClientMapping.billing_email`
  right after every sync. See §6.10 for why a changed email clears the cached Stripe customer.

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

## 5. Agency admin page (built)

The embedded admin interface Jerry opens from the GHL left nav. An agency admin sees every client,
its usage and bill status by period, and can save a client's **mapping** and **rates**. A
sub-account opening its own page sees the same client's figures, read-only.

**Routes:** `/admin` is live data behind GHL SSO. `/admin/preview` is bundled sample data
(`demoData.ts`), behind an amber "Standalone preview" banner, and **never calls the API**. Both use
`ClientBrowser.tsx` for rendering, so they look the same. The preview is for development and
walkthrough only, not for testing inside GHL.

### 5.1 How GHL identifies the viewer

The page runs in a GHL iframe, posts `{message: 'REQUEST_USER_DATA'}` to `window.parent`, and gets
`REQUEST_USER_DATA_RESPONSE` with an encrypted blob in `payload`. `apps/ghl/sso.py` opens it:
OpenSSL-compatible AES-256-CBC (`Salted__` + 8-byte salt, key and IV from EVP_BytesToKey with MD5)
using `GHL_APP_SSO_KEY`, the app's **Shared Secret** (Advanced Settings > Auth). Tests verify against
real `openssl enc` vectors.

**Scope comes from `activeLocation`.** GHL reports `type: "agency"` even inside a sub-account, so
reading `type` alone would show one client every other client's numbers. The real identifier is
`activeLocation`. For the agency account itself, `activeLocation` is the agency's own `location_id`
(`AGENCY_LOCATION_ID`). `SSOContext.scope` compares it to decide `AGENCY` or `LOCATION` scope.
**Agency scope** sees all clients and can write; **location scope** sees only that sub-account's
own page and can write nothing. `activeLocation` is the credential that prevents cross-account
viewing.

### 5.2 What the client detail returns

`GET /api/rebilling/admin/clients/<location_id>/?month=` returns, for a mapped client:

- Identity and mapping: `location_id`, `client_name`, `mapped: true`, `active`, `billing_mode`,
  `retell_agent_ids`, `retell_agents` (the same ids, each with its Retell name or `''`), `phone_numbers`,
  `twilio_subaccount_sid`.
- Who pays: `owner_name`, `billing_email`, `stripe_customer_id`, `stripe_customer_name`,
  `billing_failure_reason`.
- Rates: `rates` (the rate in effect at the end of the month), `rate_history`, `default_rates`
  (`25` and `5`, the numbers "Reset to defaults" writes back).
- Money: `bill` (`{status, total_cents, failure_reason}` for that month, or `null`), `usage`
  (minutes, costs, `estimated_bill_cents`, `unbillable_calls`, Twilio freshness).
- Detail: `twilio_days`, and `recent_calls`. Each call carries `billing_status`, `unbillable_reason`,
  and `bill_status` (the status of the bill **that call** is on, or `null`).

For a sub-account viewing its own **unmapped** page the response is
`{month, location_id, client_name, mapped: false}`, with no usage fields. For an agency session
asking about an unmapped client it is a 404: the list already marked that client unmapped, so there
is nothing to show. The frontend skips the request in that case and builds the same shape from the
list row.

### 5.3 Endpoints and who may call them

- `POST /api/ghl/sso/session/`: who is viewing (name, email, role, scope, active location).
- `GET /api/rebilling/admin/clients/` and `…/<location_id>/`: any valid session. A location session
  is refused (403) for any other client. The list is narrowed to its own client.
- `POST /api/rebilling/admin/clients/sync/`: agency only; 403 for a sub-account session.
- `PUT …/<location_id>/mapping/`: agency only (403 for a sub-account, even its own client). 404 for a sub-account that
  has not been onboarded. Takes `twilio_subaccount_sid`, `retell_agent_ids`,
  `phone_numbers`. Never `active`, `billing_mode`, rates, or `billing_email`.
- `PUT …/<location_id>/rates/?month=`: agency only (403 for a sub-account, even its own client). 404
  if the client has no mapping. Both `voice_rate_cents` and `call_rate_cents` are **required**
  non-negative integers (cents). Answers with the client's whole refreshed detail for `month`.
- `GET /api/rebilling/admin/retell-agents/` and `…/twilio-subaccounts/`: agency only. Read the live
  provider accounts. A provider failure is a 502.

All the admin endpoints authenticate with the SSO blob in the `X-GHL-Session` header. The blob is the
credential. It is opened per request, with no cookie, no JWT and no session store.

### 5.4 The client page, as an agency admin sees it

- **Headline tile.** Before a bill exists it is labelled **Estimated** (`minutes × current rate`).
  Once the month has a bill, the tile is labelled with the bill's status (**Uncharged**, **Charging**,
  **Paid**, **Failed**) and shows the amount the client was actually charged. The margin is then
  worked out from that billed figure, not from the estimate. A note under the tile says which figure
  it is.
- **Failure reason.** If the bill carries a `failure_reason`, it is shown in an alert whatever the
  status. A charge that never reached Stripe (no matched customer) leaves the bill `uncharged` with the
  reason recorded, and that reason is the only thing telling an admin what to fix.
- **Rates card.** Two inputs, AI minute rate and Call minute rate, typed in dollars and saved in cents.
  - **Save rates** sends both rates. The server starts a new rate **today** (see §6.8). The response
    is the refreshed detail, and the page shows the server's own figures.
  - **Reset to defaults** fills both inputs with `default_rates`. It does **not** save. Saving after it
    is what makes the defaults the client's active rates.
  - While the typed rates differ from what is saved, the estimate is recomputed in the browser and the
    rates are marked "not saved". Once saved, the component remembers what it wrote, because a rate
    saved today does not move the rate shown for a month that has already ended.
  - A failed save keeps the typed rates on screen and says "Could not save the rates. Try again."
- **Mapping.** The chip list shows each agent by its name. The agent picker lists agents by name, marks
  those already on the client as "Added", and fetches the list the first time it is opened. Phone numbers
  are typed. The Twilio subaccount is picked from a list, with a "No Twilio subaccount" row that takes the
  client off regular-call billing.
- **Recent AI calls.** Each billable call's pill follows **its own bill**: `Billable` while no bill has
  taken it, `Billed` while its bill is uncharged, charging or failed, and `Paid` once its bill is paid.
  An unbillable call shows a short reason, with the full sentence on hover. The month's bill cannot answer
  this for each call: a call made after the run charged the month is on no bill and is still billable.
- **Billing contact card.** Stripe customer's name (or "No name on file in Stripe", or "Matched on the
  first charge" when no customer is matched yet), billing email, and the sub-account row: the GHL owner's
  name, or the client's own name when GHL has no owner on file. Never a raw id.

### 5.5 As a sub-account sees it

The same page, read-only. Usage, bills, and the calls table are shown. The rates appear as text, with no
inputs and no buttons. The mapping appears as text, with agent names. The billing contact card is shown.
A sub-account's own page never reaches the client list, and has no "All clients" button.

### 5.6 Period filter

`MonthPicker.tsx`: arrows and a list to step or jump between months. Bounded at the current month (no
future billing) and at the oldest listed month, so the selected value always exists. It drives both the
detail request and the list request, through `?month=`.

---

## 6. Task 4: voice rebilling (Phases 1, 2, 2b, 3, 5 built; Phase 4 mostly built; Phase 6 pending)

### 6.1 The business rule

Retell and Twilio each bill Jerry for everything as one bill. We split that usage per client (per
GHL sub-account), apply the rates Jerry set for that client, and bill each client separately from
their $397 plan. Example: Cool Breeze, 200 AI minutes at $0.25 plus 100 regular call minutes at
$0.05 = a $55 usage bill.

**The two ingest paths share exactly one thing: `ClientMapping`.** Retell is one row per call,
written once. Twilio is one row per client per GMT day, re-written on every pull. Do not unify
them. Do not apply Retell's rounding rule to Twilio.

### 6.2 Models (`apps/rebilling/models.py`)

| Model | Purpose | Key fields |
|---|---|---|
| `ClientMapping` | One client. Links the GHL sub-account to Retell agents, phone numbers and the Twilio subaccount | `location_id` (unique), `client_name`, `retell_agent_ids` (JSON list), `phone_numbers` (JSON list, digits-only match), `twilio_subaccount_sid`, `billing_mode` (`auto_charge` default, or `send_invoice`), `billing_email` (copied from GHL, never typed), `stripe_customer_id` (matched by email, never typed), `billing_contact_id` (**exists on the model, nothing reads or writes it; do not use**), `active` |
| `RateHistory` | Rates per client from a start date. A change **adds a row** | `voice_rate_cents` (default 25 = AI minute, $0.25), `call_rate_cents` (default 5 = call minute, $0.05), `start_date`. Unique on `(client, start_date)` |
| `RetellCall` | One Retell call matched to a client | `call_id` (unique), `client` (FK, **PROTECT**), agent, numbers, direction, `started_at`, `ended_at`, `duration_ms`, `retell_cost_cents` (= `call_cost.combined_cost`, Jerry's cost), `call_status`, `billing_status`, `unbillable_reason`, `bill` (FK to `Bill`, **SET_NULL**) |
| `UnassignedCall` | Same call fields, no client. Matched nothing. Never dropped | same as `RetellCall` minus `client` and `bill` |
| `TwilioUsageDay` | One client, one GMT day, one category | `client` (FK, **PROTECT**), `subaccount_sid`, `usage_date`, `category` (`calls`), `call_count`, `usage_minutes` (Decimal, already rounded by Twilio), `twilio_price_cents` (Jerry's cost), `as_of` (Twilio's freshness stamp), `bill` (FK, **SET_NULL**). Unique on `(client, usage_date, category)` |
| `Bill` | One client's bill for one month | `client` (FK, **PROTECT**), `billing_period` (`YYYY-MM`), `total_cents`, `status` (`uncharged` default, `charging`, `paid`, `failed`), `stripe_invoice_id`, `charged_at` (when the card was first attempted), `paid_at` (Stripe's timestamp), `failure_reason` (Stripe's own error code, or a plain reason when no charge was attempted). Unique on `(client, billing_period)` |
| `BillLine` | One line on a bill: Retell or Twilio | `bill` (FK, CASCADE), `line_type`, `quantity_minutes`, `cost_cents` (Jerry's cost), `bill_cents` (what the client is charged) |

**Migrations:** `0001_initial`, `0002_call_billing_status`, `0003_twilio_voice_usage`,
`0004_bill_and_billline`, `0005_bill_stripe_fields_and_billing_email`,
`0006_bill_charged_at_and_failure_reason`. After any model change, `makemigrations --check --dry-run`
must report "No changes detected".

`0006` is written defensively. Another branch (`feat/ghl-billing-with-stripe-fallback`) also adds a
`charged_at` column in its own `0006`, so a database that has run that branch already has the column.
The migration checks for it before adding it. `failure_reason` has no such guard, because only this
branch adds it.

### 6.3 Pure functions that carry the rules

- `billable_minutes(duration_ms)` returns per-second (`Decimal`) or round-up-to-minute, from
  `RETELL_ROUNDING`. Retell only.
- `billing_state(duration_ms, started_at, call_status)` returns `(billing_status, reason)`. A call
  is **unbillable** when its status isn't `ended`, when it has no duration, or when it has no start
  time. Each case has a plain-English reason. It runs on every store and every re-read.
- `normalise_number(value)` keeps digits only, for phone matching.

### 6.4 Retell path

**Webhook** `POST /api/rebilling/webhooks/retell/<location_id>/` (`apps/rebilling/views.py`):

1. Reads `request.body` **before anything parses the stream**. The signature covers the raw bytes.
2. Looks up that client's `RetellWorkspace` by the id in the path. No row → 401, nothing stored.
3. `retell.lib.verify(raw_body, workspace.api_key, X-Retell-Signature)`. 401 if the signature is
   bad, 400 if the JSON is bad. **The key is the signing secret**, which is why the client has to
   come from the URL: the right key cannot be chosen until the client is known.
4. Stamps `last_webhook_at` on **any** verified event, not only `call_ended` — it is the only
   evidence the webhook-badged key is the one we hold.
5. `call_ended` goes to `store_workspace_call()`.
6. Always answers **204 with an empty body**. Retell allows 10 seconds and retries up to 3 times
   on a non-2xx.

**No matching.** The workspace belongs to exactly one client, so the URL (or, in the audit, the
key that fetched the call) has already said whose it is. A call whose `agent_id` is not on the
client is **stored against them anyway**, the agent appended to `retell_agent_ids` and a warning
logged — never rejected, never unassigned. An agent the GHL team created an hour ago is not in our
list yet, and rejecting would drop a new client's first real calls.

**Storing** (`store_workspace_call()`): idempotent on `call_id`. Retell's retries and the nightly
audit both rely on this. First write wins.

**Nightly audit** `reconcile_all()` → `RetellReconciliationService(key).reconcile_workspace(ws)`:

- One pass **per workspace**, strictly **serial**. Retell's `list-*` budget is 30 req/10 s "per
  organization + route" and its docs say not to parallelize those calls; whether a workspace is
  its own organization is undocumented, so serial is correct either way.
- `POST /v3/list-calls`, `limit` 1000, paging on `pagination_key` until `has_more` is false. It
  stops if `has_more` is true but no key comes back.
- The window starts at that workspace's `last_call_timestamp_ms` less `RECONCILE_OVERLAP_HOURS`
  (1), not a fixed 48 hours — re-listing two days per client every night is the same data fetched
  N times over. A mark older than the window is **not** clamped back to it. A workspace nothing
  has listed yet falls back to `RECONCILE_WINDOW_HOURS`.
- Every call goes through `store_workspace_call()`.
- Backfill: for **that client's** calls inside the fixed 48-hour window that are unbillable or
  missing a cost, `GET /v2/get-call/{call_id}`, refreshing duration, cost, status and `ended_at`
  and re-running `billing_state()`. Scoped to one client because the key is.
- Errors: a 429 is waited out **once** for `RateLimit-Reset` seconds, capped at 10. Anything else
  raises `RetellError`, which marks that workspace `key_invalid` and moves to the next one. The
  command exits non-zero only when **every** workspace failed.
- A list that answered proves the key, so it also sets `status` and `last_checked_at`. The daily
  health check and the nightly audit are the same request.
- Auth: `Authorization: Bearer <that workspace's key>`.
- Duration is read from `duration_ms`, else `duration_seconds`, else
  `call_cost.total_duration_seconds`.

**Why the backfill is limited to the window:** a call that never connected has no duration. Without
the window it would be re-fetched every night, forever.

**Agent names** (`RetellReconciliationService.list_agents()`, `POST /v2/list-agents`, paged on
`pagination_key`): read with **that client's own key**, so it lists one workspace rather than the
whole account. Used by the key test (where the names are shown so a person can confirm the key
belongs to this client) and by the client detail, which names each agent on every view. A client
with no key is not asked at all. See §16 for the cost of that.

### 6.5 Twilio path

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

### 6.6 Schedules and commands

| What | Schedule (UTC) | Celery task | Manual command |
|---|---|---|---|
| Retell audit | 03:00 daily | `apps.rebilling.tasks.reconcile_retell_calls` | `python manage.py reconcile_retell_calls` |
| Twilio pull | 03:30 daily | `apps.rebilling.tasks.pull_twilio_usage` | `python manage.py pull_twilio_usage [--start YYYY-MM-DD --end YYYY-MM-DD]` |
| Calculate bills | **not scheduled** | — | `python manage.py calculate_bills [--month YYYY-MM]` |
| Charge bills | **not scheduled** (deliberately) | — | `python manage.py charge_bills [--month YYYY-MM]` |
| Retry declined charges | Hourly, at :15 | `apps.rebilling.tasks.retry_failed_charges` | `python manage.py retry_failed_charges` |

- The beat schedule lives in `CELERY_BEAT_SCHEDULE` in `config/settings/base.py`.
  `config/celery.py` is the Celery app. `config/__init__.py` exposes it so `@shared_task` binds.
- Running the schedule needs **both** `celery -A config worker` and `celery -A config beat`, plus a
  reachable Redis (`REDIS_URL`). Nothing runs without them. Whether they run on the server is not
  recorded here; check before relying on the nightly audit or pull.
- The Retell command exits non-zero on failure. The Twilio command exits non-zero **only when every
  client fails**, which points to a systemic problem such as bad credentials or Twilio being down.
  A partial failure warns and exits 0. Cron can monitor the exit code.
- The **first** charge is never automatic. It runs by hand, per month, in order: the Twilio month-close pull
  (with both dates), then `calculate_bills`, then `charge_bills`.

### 6.7 Rebilling settings

| Setting | Default | Notes |
|---|---|---|
| ~~`RETELL_API_KEY`~~ | — | **Removed (Phase 9).** Each client's key lives encrypted on `RetellWorkspace` and is pasted on their page. It must still be that workspace's **webhook-badged** key, which also verifies its signatures |
| `RETELL_ROUNDING` | `per_second` | or `round_up_minute`. **Retell only** |
| `RETELL_API_BASE_URL` | `https://api.retellai.com` | |
| `RETELL_REQUEST_TIMEOUT` | 30 | seconds |
| `TWILIO_ACCOUNT_SID` | none | The **parent** account's SID |
| `TWILIO_AUTH_TOKEN` | none | The **parent** account's auth token. A plain env setting, not encrypted in the database (decided; see plan.md) |
| `TWILIO_API_BASE_URL` | `https://api.twilio.com` | |
| `TWILIO_REQUEST_TIMEOUT` | 30 | seconds |
| `TWILIO_BILLING_ENABLED` | `False` | Whether Twilio call minutes are billed. Off: GHL rebills them natively. The pull still runs; only billing and the UI are paused |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker |
| `STRIPE_SECRET_KEY` | none | The API key of the Stripe account bills are charged through. See §15 |
| `STRIPE_WEBHOOK_SECRET` | none | The webhook signing secret. Different from the API key |
| `BILLING_MAX_RETRIES` | 3 | Automatic retries after a declined charge, before the bill is `failed` |
| `BILLING_RETRY_INTERVAL_HOURS` | 12 | Hours between automatic retries |
| `GHL_RETELL_STATUS_CUSTOM_FIELD_ID` | none | The custom field on a billing contact set to `failed` on a final failure and `active` on payment. Blank until Jerry creates the field; the write fails and is logged until then |
| `GHL_CARD_STATUS_CUSTOM_FIELD_ID` | none | The custom field on a billing contact set to `card not found` when an admin asks for a card. An **id**, which works here because every billing contact is in the one main account. Blank until Jerry creates the field; a card request fails until then |
| `GHL_CARD_WEBHOOK_TOKEN` | none | The shared secret the GHL workflow sends in `X-Card-Webhook-Token` when a card has been saved. **Blank refuses every request** |

### 6.8 Calculation (built)

`calculate_bills(month=None)` (`apps/rebilling/services.py`) creates one `Bill` per active client per
month, with a `BillLine` per source that has billable usage.

- **AI-minute line:** sums `billable_minutes` over `RetellCall` rows with `billing_status = billable` in
  the month, each at the `voice_rate_cents` in effect **on that call's day**. A mid-month rate change
  splits the bill correctly. A call on a day with no `RateHistory` row yet is logged and left out of this
  bill (not marked billed), rather than failing the whole run.
- **Call-minute line:** sums `TwilioUsageDay.usage_minutes` in the month, each at the `call_rate_cents`
  in effect **on that `usage_date`**. Never rounded again.
- A client with no `twilio_subaccount_sid` gets no Twilio line. Retell billing for everyone else is
  unaffected.
- All arithmetic in `Decimal`. The final cents total is rounded once with `ROUND_HALF_UP`, never
  truncated and never float.
- A client with nothing billable from either source gets no `Bill` at all. Never a zero bill.
- `RetellCall.bill` and `TwilioUsageDay.bill` are set only on the rows included in a bill. Deleting a
  `Bill` unlinks those rows (`SET_NULL`) and never deletes the call or usage-day records.
- Rate lookup fetches a client's `RateHistory` once per run, not once per call.
- `calculate_bills` does not trigger the Twilio month-close pull. Run the two in order.

**Saving a rate** (`set_rates()`, `services.py`, used by `PUT …/rates/`): writes a `RateHistory` row
dated **today**, or updates today's row if one already exists (`update_or_create` on `(client, start_date)`).
Because a rate is looked up as the row in effect on a given day, a month that has already ended keeps the
rate it had then. A rate change therefore never moves a past bill or a past month's figures.

**Tests:** `apps/rebilling/tests/test_billing.py` covers rate-in-effect billing, mid-month rate splits,
unbillable exclusion, Twilio no-double-rounding, single-source clients, zero-usage clients, inactive clients,
idempotent re-runs, the integer-cents invariant, `SET_NULL` on bill deletion, and the command. The rates
endpoint is covered by `test_admin_rates.py`.

### 6.9 Phase 4: the admin page's write half

| Phase | What | Status |
|---|---|---|
| 1 | Models, client mapping, Retell webhook | Built |
| 2 | Retell nightly reconciliation (list and backfill) | Built |
| 2b | Twilio daily usage pull (`calls` only) | Built |
| 3 | Calculation and bills, both sources, typed bill lines, integer cents | Built (§6.8) |
| 4 | Admin page: mapping edit, rates edit and reset, bill status, per-call status, names | **Mostly built.** Not started: a UI for mapping unassigned calls; a bills table |
| 5 | Charging through Stripe, webhook for payment events | Built (§6.10) |
| 6 | Alerts (failed pulls, unassigned calls, failed charges, bills stuck in `charging`), then the Branwynn end-to-end test | **Not started** |
| 7 | Card on file: Stripe check, status in the UI, GHL card request | Built (§6.11). The rebilling status write moved to a contact field with it (§6.10b) |

**Mapping** (`ClientMappingSerializer`, `set_mapping()`, `AdminClientMappingView`): agent ids non-blank and
deduplicated; phone numbers 7 to 15 digits, deduplicated, normalised; Twilio SID matches `^AC[0-9a-fA-F]{32}$`
(blank allowed). `set_mapping()` writes only the fields sent.

**Mapping editor** (`MappingEditor.tsx`): one component for the live page and the preview. On save: preview
writes to an in-memory store in `demoData.ts` (`savePreviewMapping`); live calls `saveMapping`. An unmapped
client gets the same editor in its "Set up for billing" card.

**Rates editor** (`ClientDetail.tsx`, `saveRates` in `api/admin.ts`): see §5.4. The preview writes rates to an
in-memory store (`savePreviewRates`, `resetPreviewRates`) and never calls the API.

**Unmapped clients and the 404.** `AdminPage.tsx` skips the detail request when the list already marks a
row `mapped: false`, and builds the unmapped shape from the list row. This keeps the agency page from
racing the 404 described in §5.2.

### 6.10 Phase 5: charging through Stripe (built)

**Why Stripe.** Stripe is the charging rail for this branch. A GHL one-off invoice was tried first (on
the other branch): its auto-charge never ran, because GHL's auto-pay is scoped to recurring invoices and
the endpoint takes no card reference this app can supply. GHL's wallet pays out monthly, which would
mean financing about 45 days of Retell and Twilio cost. Stripe invoices give an itemised, customer-facing
artifact, with one line per source.

**Customer matching** (`customer_id(client)`, `stripe_billing.py`):

- A cached `stripe_customer_id` is used as is.
- Otherwise it looks the customer up by `billing_email` (`stripe.Customer.list(email=…)`), caches the id
  it finds, and returns it.
- **It never creates a customer.** A customer created here would have no card, and the charge would fail
  at the card for a reason nobody could act on. If nothing matches, it raises
  `No Stripe customer found for billing email: <address>`, and that message is recorded on the bill.
- **A changed billing email clears the cached id** (`sync_billing_emails()`). Keeping it would go on
  charging the previous payer's card.

**Card check** (`_assert_chargeable(customer)`): the customer must have an invoice default payment method,
or the legacy `default_source`. Without that check, an invoice finalises and then fails at the card, which
looks exactly like a decline.

**Charging** (`charge_bills(month)`, `_charge_one(bill)`):

- Picks up only `uncharged` bills for the month. Skips any client whose `billing_mode` is not `auto_charge`
  (counted as `skipped`; nothing is sent to them, see §16).
- A bill under `MINIMUM_CHARGE_CENTS` (50¢, Stripe's minimum for USD) is counted as `too_small` and left
  `uncharged`.
- For the rest: `Invoice.create(collection_method='charge_automatically', auto_advance=True)`, then one
  `InvoiceItem` per `BillLine` (integer cents, with a description of what was bought), then
  `finalize_invoice`. The invoice id is saved as soon as it exists, before the lines.
- After finalising, the bill is `charging`, `charged_at` is set to now, and `failure_reason` is cleared.
- **Idempotency:** every Stripe call carries a key derived from `Bill.pk`: `bill-{pk}-invoice`,
  `bill-{pk}-{line_type}`, `bill-{pk}-finalize`. A re-run re-reaches the same objects. Only `uncharged`
  bills are picked up at all, so a re-run cannot charge a bill twice.
- A per-client failure (`StripeBillingError`, or a Stripe error) is logged and counted as `failed`. The
  reason is written to `failure_reason`, and the bill **stays `uncharged`**. Only the webhook sets `failed`.
- Returns `{period, charged, skipped, too_small, failed, total_cents}`.

**Webhook** (`POST /api/rebilling/webhooks/stripe/`, `StripeWebhookView`):

- Reads the raw body first. A bad signature answers 401; invalid JSON answers 400.
- `invoice.paid`: the bill becomes `paid`, `paid_at` is Stripe's event time (not ours, since a webhook can
  be retried hours later), and `failure_reason` is cleared (Stripe's own retries can collect a bill that
  failed once).
- `invoice.payment_failed`: the bill becomes `failed`, with Stripe's own error code as `failure_reason`
  (`card_declined`, `authentication_required`, and so on). That code is what separates "send a new card"
  from "ask them to confirm this one".
- A `paid` bill ignores later failure events (webhooks are not ordered). An event for an unknown invoice is
  logged and ignored. Other event types are ignored. A valid signature always answers 204.

**Display name and card** (`customer_facts(client)`): one `stripe.Customer.retrieve`, read live on each
admin detail view, answering both halves of the billing contact card. See §6.11. A Stripe error is logged
and comes back as `unknown`, so a Stripe outage cannot break the page.

**Setup link** (`setup_link(client)`): a Stripe Checkout session in `mode='setup'`, for a client with no card.
Nothing calls it from a UI yet.

**Settings:** `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`.

**Tests:** `test_stripe_billing.py` (every Stripe call faked by `FakeStripe`, and a guard that fails any
unfaked call by name), and `test_billing_email.py` (who pays, and the cached-customer rules).

### 6.10b The rebilling status write: a contact field, not a custom value (built)

`sync_rebilling_status(bill)` is what tells GoHighLevel to pause or resume a client's Retell agents:
`failed` once a bill has used up its retries, `active` once one is paid — unless another bill of
theirs is still `failed`, since paying one month must not switch the agents back on while an earlier
month is unpaid. Any other status leaves GHL alone, so a bill still being retried pauses nobody.

**It wrote a custom value in the client's own sub-account, and nothing ever happened.** GoHighLevel
workflows have no trigger for a custom value changing; they trigger on a *contact* changing. So the
status was written faithfully, every month, into a field no workflow could watch.

It now writes to a custom field on the client's **billing contact in the main account**
(`AGENCY_LOCATION_ID`), found by `billing_email` — the same contact and the same
`GHLOAuthService.set_contact_field()` the card request uses (§6.11), so there is one way a client's
status reaches GHL, not two.

- **Setting:** `GHL_RETELL_STATUS_CUSTOM_FIELD_ID`, replacing `GHL_RETELL_STATUS_CUSTOM_VALUE_NAME`.
  An id rather than a name, which works for the same reason the card field's does: every billing
  contact is in the one sub-account, so there is one field and one id.
- **Still best effort.** The bill's own outcome is already saved, and a GHL failure must not undo it
  or stop the webhook or the retry run, so `GHLError` is logged rather than raised. That is the one
  difference from the card request, which raises because a human is waiting on the answer.
- **A client with no `billing_email` has no contact to write to**, so the write is skipped and
  logged. Nothing is invented: a guessed contact would tell a workflow about the wrong client.
- **Gone with it:** `GHLOAuthService.set_custom_value()`, its four tests, and the
  `locations/customValues.readonly` / `.write` scopes — nothing reads or writes a custom value any
  more, so the re-authorisation asks for less than it did.
- **Tests:** `test_retries.py` asserts the `(email, field_id, value)` triples recorded by the autouse
  `ghl_contact_writes` fixture, which is now the only GoHighLevel seam in the rebilling package.

---

### 6.11 Phase 7: card on file, and asking for one (built)

Until this, the only thing that ever asked whether a client could be charged was `charge_bills`, at month
end. Now the question is answered on the client's own page, the moment they are mapped, and asking them
for a card is a button.

**The status is read from Stripe on every view, never stored.** A saved answer about whether money can be
taken is wrong from the moment the client enters a card. `customer_facts(client)`
(`stripe_billing.py`) makes **one** `Customer.retrieve`, expanded on
`invoice_settings.default_payment_method` — the same call the billing contact card was already making for
the customer's name — and returns the name, the id and `{status, brand, last4}`. It is reached through
`billing_facts(mapping)` in `services.py` (the old private `_billing_link`), which adds `requested_at`.

| Status | What Stripe said | On the page |
|---|---|---|
| `card_on_file` | A default payment method, or the legacy `default_source` | Green pill, `Visa •••• 4242` |
| `no_card` | A customer, but nothing Stripe would collect against | Amber pill, **Request card** |
| `no_customer` | No Stripe customer matches `billing_email` | Amber pill, **Request card** |
| `no_billing_email` | Nothing was looked up — GHL has no owner email | Amber pill, no button |
| `unknown` | Stripe could not be reached | Muted "could not check", no button |

Two of these are worth knowing about. **A card attached but not set as the invoice default reads as
`no_card`**, because that is what `charge_bills` would hit — the test is `_assert_chargeable`'s, so the
page and the charge can never disagree. And **`unknown` is never shown as `no_card`**: chasing a client
who has a perfectly good card is worse than admitting Stripe was unreachable.

The page now **matches the Stripe customer on first view** rather than on first charge, since answering the
card question needs one. That is `customer_id()` unchanged, including its refusal to create a customer that
matched nothing.

**Requesting a card** (`request_card(client)`): writes `card not found` to the card status **custom field on
the client's billing contact**, which is what starts Jerry's GoHighLevel workflow emailing them a form.
`GHLOAuthService.set_contact_field(email, field_id, value)` finds the contact by email in the **main
account** (`GET /contacts/search/duplicate`) and writes it (`PUT /contacts/{id}`, `fieldValue`), both
`Version: v3`, both on a Location-class token minted on demand by `get_main_location_token()` and
deliberately **not stored** — a stored row would show the agency's own location in the billing dashboard as
a client, and an agency token is refused by `/contacts/` outright. Unlike the rebilling status write, this
one **raises**: a human pressed a button, so a failure is shown, not logged.

`ClientMapping.card_requested_at` (migration `0008`) is the only thing stored, because whether *we asked*
is the one part Stripe cannot tell us.

**Endpoint:** `POST /api/rebilling/admin/clients/<location_id>/card-request/`, agency scope only (this
sends the client an email; 403 for a location session even on its own client), 404 unmapped, 502 when
GoHighLevel refuses. It answers with the card status **re-read from Stripe**, so a client who attached a
card between the page loading and the button being pressed comes back green rather than chased.

**Webhook:** `POST /api/rebilling/webhooks/ghl-card/`, called by the workflow once the client has entered a
card. It is **not** signed by GHL — a workflow's Custom Webhook action is an arbitrary request, not a
marketplace event — so it authenticates on `GHL_CARD_WEBHOOK_TOKEN` in `X-Card-Webhook-Token`, compared
with `hmac.compare_digest`. **A blank setting refuses everything.** What it is told is never believed:
Stripe is re-read, and only Stripe's answer clears `card_requested_at`. 401 bad token, 400 unreadable body
or no `locationId`, 204 otherwise, including for a sub-account we do not bill.

**Scope:** `contacts.write` is new in `GHL_SCOPES`. **The app must be re-authorised** before a card request
can work.

**Tests:** `apps/rebilling/tests/test_card_status.py` (30), five more in `apps/ghl/tests/test_services.py`
for `set_contact_field`, and a new autouse `ghl_contact_writes` fixture so nothing in the rebilling package
can reach GoHighLevel. Frontend: a `card on file` block in `AdminPage.test.tsx` and two preview tests.

---

## 7. Financial safeguards

These are enforced in code, or are explicit requirements.

1. **Never drop a call.** Anything that can't be billed is stored as `billing_status = unbillable` with a
   plain-English `unbillable_reason`.
2. **The default is unbillable.** A row created by hand in the shell is never billed until it has been
   checked.
3. **Duration comes from three places** (`duration_ms`, `duration_seconds`, `call_cost`), so a call isn't
   lost to the unit it arrived in.
4. **A missing cost does not block billing.** `retell_cost_cents` is Jerry's cost, used for margin only.
5. **Integer cents for stored money.** `Decimal` for minutes. Never `float` for money. Twilio's string numbers
   are parsed as `Decimal`.
6. **Rounding is per source.** `RETELL_ROUNDING` applies to Retell only. Twilio arrives pre-rounded.
7. **No double-billing.** Bill Twilio `calls` only. Retell lands in the trunking categories. On top of that, Twilio billing is **paused** entirely (`TWILIO_BILLING_ENABLED`), because GHL rebills those minutes natively: `calculate_bills` builds no Twilio line, and `charge_bills` refuses to charge a bill calculated before the pause that still carries one, counting it as `twilio_paused` and recording the reason rather than reducing the total (the invoice and the saved bill must not disagree).
8. **Re-pulls are safe.** Twilio uses `update_or_create`. Retell uses first-write-wins on `call_id`.
9. **Never charge the same period twice.** `Bill` has `unique_together(client, billing_period)`. Charging
   picks up only `uncharged` bills, and every Stripe call carries an idempotency key derived from the bill.
10. **Save the bill and its lines before charging**, so any number can be explained later. `calculate_bills`
    does this; `charge_bills` reads the saved bill and never recomputes it.
11. **Rates have history.** A rate change adds a `RateHistory` row, and usage before the change keeps the old
    rate. Both `calculate_bills` and the admin page look each rate up by the day of the call or usage date.
12. **A failed Twilio pull must not look like zero usage.** The `failed` count and the command's non-zero exit
    exist for this. Phase 6 should alert on it.
13. **A failed page abandons that client's whole window.** A half-read window would look settled.
14. **A billed month cannot be moved by a rate typed on the page.** The bill was worked out from the rates in
    effect at the time, and the page says so.
15. **A failed charge is never hidden.** `failure_reason` is shown whatever the bill's status, because a charge
    that never reached Stripe records its reason and stays `uncharged`.

---

## 8. Money model

- A monthly billing period (`YYYY-MM`), via the existing `month_window()` helper.
- Per active client, per period:
  - **AI-minute line:** sum the `billable_minutes` of `billable` `RetellCall` rows, each at the
    `voice_rate_cents` in effect on the call's day.
  - **Call-minute line:** sum `TwilioUsageDay.usage_minutes`, each at the `call_rate_cents` in effect on
    the `usage_date`.
- Integer cents throughout. Typed bill lines, so Retell and Twilio stay distinguishable.
- Margin comes from `retell_cost_cents` and `twilio_price_cents`, both stored.
- Clients with nothing to bill are skipped.
- Bills and their lines are saved before any charge.

---

## 9. Frontend

- `frontend/src/App.tsx` holds the routes and `main.tsx` mounts them. The Vite config proxies `/api` to `:8000`.
- Built: the `/onboarding` page (connect button, a status banner, the sub-account list, a sync button), and
  the `/admin` and `/admin/preview` pages (§5, §6.9).
- Not built: the agency dashboard and the client dashboard (empty folders).
- `frontend/dist/` is build output and is gitignored.
- **Type checking:** the root `tsconfig.json` is solution-style (`"files": []`), so `npx tsc --noEmit` checks
  nothing. Use `./node_modules/.bin/tsc -b` from `frontend/`. Plain `npx tsc -b` may try to install an unrelated
  package.
- **Test fixtures:** `ClientDetail` requires every field, so a fixture that omits one fails `tsc -b`.
  Tests override single fields through `mockApi({ detail: {…} })`.

---

## 10. Environment variables

Everything is in `backend/.env`, which is gitignored. `backend/.env.example` documents every key.
**Never print, log or commit `.env`.** The keys it needs:

- **Django:** `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `FRONTEND_URL`,
  `DATABASE_URL` (defaults to SQLite), `REDIS_URL`.
- **Encryption:** `FIELD_ENCRYPTION_KEY` (a Fernet key). Generate it with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  **Rotating it makes every stored token unreadable**, so it needs a plan first.
- **GHL:** `GHL_CLIENT_ID`, `GHL_CLIENT_SECRET`, `GHL_APP_ID`, `GHL_APP_SSO_KEY`, `AGENCY_LOCATION_ID`,
  `GHL_REDIRECT_URI`, `GHL_INSTALL_URL_BASE`, `GHL_API_VERSION` (default `2021-07-28`), `GHL_SCOPES`,
  `GHL_API_BASE_URL`, `GHL_REQUEST_TIMEOUT`.
- **Retell:** `RETELL_ROUNDING`, `RETELL_API_BASE_URL`, `RETELL_REQUEST_TIMEOUT`. **No API key**: one per client, encrypted on `RetellWorkspace` (Phase 9).
- **Twilio:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_BASE_URL`, `TWILIO_REQUEST_TIMEOUT`.
- **Stripe:** `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`.

`GHL_COMPANY_ID` was **removed entirely**. Do not reintroduce it. `AGENCY_LOCATION_ID` replaced it as the way to
identify the agency location.

Test settings (`config/settings/test.py`) set fixed fake values for every secret. Do not copy them anywhere
else, and do not treat them as real.

---

## 11. Commands

Run these from `backend/` unless noted. The venv is `backend/.venv`. On Windows, use `.venv/Scripts/python.exe`.

```
pytest                                                    # config.settings.test (pytest.ini)
ruff check . && ruff format .                             # lint / format (line 100, single quotes)
python manage.py check
python manage.py makemigrations --check --dry-run         # must say "No changes detected"
python manage.py migrate
python manage.py runserver                                # http://localhost:8000
python manage.py reconcile_retell_calls                   # manual Retell audit
python manage.py pull_twilio_usage [--start --end]        # manual Twilio pull (rolling 3-day window by default)
python manage.py calculate_bills [--month YYYY-MM]        # build the month's bills
python manage.py charge_bills [--month YYYY-MM]           # charge the month's uncharged bills through Stripe
celery -A config worker -l info                           # background worker
celery -A config beat -l info                             # scheduler
pip-sync requirements/dev.txt                             # install dependencies
pip-compile requirements/base.in -o requirements/base.txt # after editing a .in file
```

From `frontend/`:

```
npm install
npm run dev                          # http://localhost:5173
npm test -- --run                    # vitest, run once
./node_modules/.bin/tsc -b           # type check (see §9)
./node_modules/.bin/oxlint src       # lint
npm run build                        # tsc -b && vite build -> dist/
```

---

## 12. Testing rules

- **No live HTTP calls to GHL, Retell, Twilio or Stripe, in tests or in scripts.** Every external call is
  mocked with `respx` or a test double. The user has said this explicitly and repeatedly. Do not run anything
  that could reach a real API.
- Tests run against in-memory SQLite and a fixed Fernet key.
- Shared guards: `apps/rebilling/tests/conftest.py` is the package's guard set. `test_stripe_billing.py` also
  blocks Stripe at the HTTP layer and fails any unmocked call by name.
- **respx in tests:** a test that needs a default route for an endpoint the code calls on every request (for
  example Retell's agent list, called on each client detail) uses its own `respx.mock(assert_all_called=False)`
  router, so a route nothing hits does not fail the test. Two activations of respx (its global singleton and
  a local router) can coexist: an unmocked request on either raises.
- Shared helpers: `apps/rebilling/tests/test_services.py` (`make_client`, `call_payload`) is reused by the other
  rebilling test modules.

**Counts (last run):** backend 289 passed (`apps/ghl`, `apps/rebilling`). Frontend 69 passed (3 files).

| File | Covers |
|---|---|
| `apps/ghl/tests/test_services.py`, `test_views.py`, `test_sso.py`, `test_webhooks.py` | OAuth exchange, refresh, location tokens, sync, callback, SSO vectors, location-created webhook signatures |
| `apps/rebilling/tests/test_services.py` | matching, `store_call`, idempotency, billing state, rounding |
| `apps/rebilling/tests/test_views.py` | Retell webhook signature, 204, 400, 401, event filtering |
| `apps/rebilling/tests/test_reconciliation.py` | list and paging, backfill, errors, command |
| `apps/rebilling/tests/test_tasks.py` | Retell Celery task, beat schedule |
| `apps/rebilling/tests/test_twilio.py` | Twilio pull, paging, update vs insert, per-client failure, command, task, schedule |
| `apps/rebilling/tests/test_billing.py` | bill calculation, rate-in-effect billing, mid-month splits, integer cents, command |
| `apps/rebilling/tests/test_migrations.py` | migration `0006` re-run on a database that already has `charged_at`; `failure_reason` still added |
| `apps/rebilling/tests/test_admin_mapping.py` | mapping serializer validation, `set_mapping`, mapping endpoint auth and errors |
| `apps/rebilling/tests/test_admin_directories.py` | Retell agent and Twilio subaccount pickers, including 502s |
| `apps/rebilling/tests/test_admin_rates.py` | rates endpoint: saves from today, history preserved, reset, permissions, validation |
| `apps/rebilling/tests/test_admin_views.py` | client list and detail, bill status, per-call status, agent names, Stripe customer name, sync |
| `apps/rebilling/tests/test_billing_email.py` | billing email from GHL, cached customer rules, the detail's who-pays fields |
| `apps/rebilling/tests/test_stripe_billing.py` | customer matching, card check, invoice creation, idempotency, minimum charge, webhook signature and events |
| `frontend/src/features/admin/AdminPage.test.tsx` | SSO, live list, detail, mapping, rates save and reset, bill status, billed calls, sub-account view |
| `frontend/src/features/admin/AdminPreviewPage.test.tsx` | demo rendering, period filter, sample saves, unmapped client setup, billed months |
| `frontend/src/features/onboarding/OnboardingPage.test.tsx` | the connect page |

---

## 13. Engineering rules

`CLAUDE.md` has the full list. The essentials:

- **Build only what is asked.** No extra endpoints, fields, settings, models, admin pages or "for later"
  features. If something seems missing, list it as a suggestion and don't build it.
- **Data models are the user's decision.** Use exactly the fields given. Don't add timestamps, status
  enums, JSON blobs, flags or helper tables without asking.
- **One code path.** No fallbacks for hypothetical API variants. Pick the documented option.
- **No speculative abstractions.** No base classes, mixins, custom QuerySets or helper modules until there
  are two real users. A plain `.filter()` is fine.
- **Small error handling.** One exception per real caller distinction.
- **Security:** secrets are encrypted and never appear in URLs or responses. Don't add auth layers or signatures
  that weren't requested. Mention risks in the summary instead.
- **Tests cover what was asked**, not features that weren't requested.
- **When in doubt, do less and say what was left out.**
- Follow the shape of the reference projects named in `CLAUDE.md`. Use official docs for API facts only.
- **Working conventions:** don't commit or push unless asked. Don't touch files outside a task's stated scope.
  Don't start a new phase without being asked.

**Tooling gotchas (Windows, Git Bash):**

- Long inline `python - <<'PY'` heredocs (over about 120 lines) fail with `unexpected EOF`. Write the script to a
  file and run it, for example `python -I path/to/script.py`.
- The backend venv is `backend/.venv` (`.venv/Scripts/python.exe`), not `bin/`.
- Use `./node_modules/.bin/tsc -b`, not `npx tsc` (see §9).

---

## 14. Findings that changed the design

Full detail is in `local_folder/CriticalFindings.md`. These are the ones that would bite a new session:

- **Twilio API keys cannot read subaccount usage.** Use the parent SID and auth token. The PDF says otherwise.
- **Twilio usage minutes are pre-rounded.** Never apply `RETELL_ROUNDING` to them.
- **Twilio's `IncludeSubaccounts` defaults to `true`.** Always send `false`.
- **Retell's `call_ended` docs** define it as "all fields except `call_analysis`", which implies `duration_ms` and
  `call_cost` are present. The sample payload doesn't show them. Verify on the first real call.
- **Retell's SDK has no `Retell.verify` class method.** The function is `retell.lib.verify`.
- **Retell `call_status` has five values:** `registered`, `not_connected`, `ongoing`, `ended`, `error`. Only `ended`
  is billable.
- **`call_analysis` can arrive later than `call_ended`.** Use `call_analyzed` if a future phase needs analysis data.
- **GHL `companyId` comes from the token response**, not from config.
- **The GHL location token endpoint** (`/oauth/locationToken` vs `/oauth/location-token`) differs between doc
  generations. Verify on Jerry's account.
- **GHL's `Version` header** is the real API-version mechanism, not the "v2" or "v3" label in the prose.
- **GHL's invoice auto-charge does not charge one-off invoices** (tested live on the other branch). That is why
  charging here goes through Stripe.
- **GHL publishes no charge-failed webhook.** A failed charge through GHL would look like a bill that never
  finishes. Stripe does send `invoice.payment_failed`.
- **Stripe refuses charges under 50 cents** (`amount_too_small` at charge time).

---

## 15. Open questions and live verification

These cannot be answered from the docs.

**Blocks charging anyone (the most important):**

- **Which Stripe account?** `STRIPE_SECRET_KEY` must be the account that holds the clients' cards. Lookup is by
  billing email on that account. If the cards live on a different account, no customer is ever found, and every
  bill stays `uncharged` with "No Stripe customer found". Confirm which account GoHighLevel is connected to
  before the first live charge.
- **The webhook must be registered in Stripe** for `invoice.paid` and `invoice.payment_failed`, pointing at this
  app's endpoint, with `STRIPE_WEBHOOK_SECRET` matching. Without it every charge succeeds at Stripe while every
  bill here reads `charging` forever. Whether this is done is not recorded here.

**Blocks trusting Twilio at all:**

- Is every client on **their own Twilio subaccount**, as Branwynn is? If not, Usage Records cannot attribute calls
  to a client. The fallback is per-call `Calls.json` matched by phone number, a materially different build
  (described in `plan.md`).
- Jerry must supply the **parent Account SID and Auth Token**, or grant an admin invite. A main-account API key is
  not enough.

**Verify with one real day of Twilio data:**

- Retell minutes appear only under the `trunking-*` categories, never under `calls`.
- Every Retell-connected number is actually on the Elastic SIP trunk. A number connected any other way bills as a
  regular staff call.
- Twilio's `usage` for `calls` is whole minutes, already rounded per call.

**Verify on the first real Retell call (now per client — see `plan.md` Phase 9):**

- `call_ended` carries `duration_ms` and `call_cost`.
- No agent has its own `webhook_url` overriding its workspace's. `list-agents` reports neither
  `webhook_url` nor `webhook_events`, so this needs `GET /v2/get-agent/{id}` per agent.
- Each agent's `webhook_events` includes `call_ended`.
- **The saved key is that workspace's webhook-badged key.** Otherwise signatures fail silently —
  and the dashboard still reads green, because any valid key of the workspace passes the Test
  button. This is now N chances to get it wrong rather than one; the client's page warns when a
  key has been saved and no webhook has ever arrived.

**Verify on Jerry's GHL account:**

- The `Version` header, the `locationToken` path casing, `installed-locations` pagination, and the white-label install
  host (`GHL_INSTALL_URL_BASE`).
- The SaaS subscription response shape (`GET /saas/get-saas-subscription/{locationId}`), which blocks Task 2's MRR KPI.

**Business questions for Jerry:**

- Is billing **inbound as well as outbound** regular calls intended? `calls` covers both, so a client pays for calls
  their own customers make to them.
- How are **transfer legs** billed? `call_cost.product_costs[].is_transfer_leg_cost` marks them. Currently the whole
  `duration_ms` is billed.
- Do clients pay **monthly or prepaid**? The backend PDF says "being confirmed".
- **What happens to a bill under 50 cents?** It stays `uncharged` and is not retried. Options (roll forward, waive, or
  charge anyway) are undecided.
- **Are `send_invoice` clients needed?** `billing_mode = send_invoice` is accepted and skipped by `charge_bills`, but
  nothing sends them an invoice (§16).
- Who receives **alert emails**? (Phase 6.)
- If SMS ever comes into scope: does Twilio's `usage` count messages or **segments**?

**Retell multi-location architecture (SUPERSEDED 4 Nov — see `plan.md` Phase 9):**

> **This section no longer describes what is built.** It concluded "one workspace, many Apps" and
> **rejected** workspace isolation for the reasons listed below. On 4 Nov Jerry's team chose the
> opposite — **one Retell workspace per sub-account** — and it was built: per-client encrypted
> key, per-client webhook URL, per-workspace nightly audit, and no account-level `RETELL_API_KEY`
> at all. The objections below were raised again before building and were overruled, which is
> their call; they are kept verbatim because they are still **true costs** of the chosen design,
> not arguments that were refuted. Jerry's team is handling the split billing separately.
>
> The research in this section about **Apps and connections** is unaffected and still accurate.

Retell's design for an agency with 50+ GHL sub-accounts is **one workspace, one App per sub-account, one agent per sub-account**, all provisioned by script. Attribution (the `agent_id` → `ClientMapping.retell_agent_ids` match) is untouched.

- **Multiple concurrent connections, same provider:** Yes. The Integrations page's Available tab shows each provider with an "Add Account" button that works after connecting once. The underlying API is `/create-app` (creates a new app with its own `app_id`), `/list-apps`, `/update-app`, `/test-app-auth` — all returning distinct connections. A connection is scoped to one GHL Location ID, with its own Private Integration Token.
- **Per-provider limit:** 20 apps per provider by default. At 50+ sub-accounts, request a custom limit from Retell before collecting tokens.
- **Tool binding:** A tool in an agent is bound to one specific connection at config time (field `app_id`). Multiple tools in one agent can use different connections. Multiple agents can use the same connection.
- **Workspace isolation (~~reject~~ — ADOPTED 4 Nov):** Retell offers workspaces, and they are hard-isolated: separate API keys, agents, phone numbers, and billing. Fifty workspaces = fifty keys, fifty audit passes, and costs split across fifty balances — **all of that is now true and accepted.** `services.py` no longer pulls every call through one key: `reconcile_all()` walks each workspace with its own key, serially, resuming from a per-workspace high-water mark. `retell_cost_cents` is per call and unaffected, so rebilling and margin are unchanged. The split balances are Jerry's team's to manage.
- **One shared agent with dynamic variables (reject for this):** Tools bind to connections at config time, not call time. The calendar can be chosen mid-call, but the CRM connection cannot. This breaks attribution (every call carries the same `agent_id`, so `match_client()` returns the first mapping and bills one client for everyone).
- **Contact sync limit (real constraint, and the only one):** Only one CRM connection per workspace can drive contact sync at a time. Under one workspace per client this stops being a limit at all, which is the strongest argument for the new topology — and, notably, **not** the argument the source PDF gave (see `CriticalFindings.md` R6).
- **Private Integration Tokens:** No API exists to mint them. Each sub-account's token must be created by hand: Settings → Private Integrations → Create new Integration, scopes (Contacts, Opportunities, Custom Fields, Calendar), copy once. Collect all 50 at once, then script the rest.
- **Verification before scaling:** (1) Call `GET /list-app-templates` to get the exact provider string for GoHighLevel. (2) Create a second app via `POST /create-app` with a different Location ID. (3) Both should appear in `GET /list-apps` with distinct `app_id` and `tenant_id`, both `connection_status: "connected"`. If the second call overwrites the first or a second row doesn't appear, it is genuinely 1:1 and this whole plan needs rethinking.

---

## 16. Known gaps and risks

- **A bill under 50 cents has no explanation on the page.** `charge_bills` counts it as `too_small` and leaves it
  `uncharged` without a `failure_reason`, so the admin page shows "Uncharged" with no reason. This is a small gap to
  close with the open question in §15.
- **`send_invoice` clients are never billed.** Their bills are calculated and then skipped. Nothing sends an invoice,
  and no UI or command shows them.
- **`setup_link()` is not reachable from any UI.** A client with no card has no way to add one from this app yet.
- **Live provider calls on every client view.** The client detail names each mapped agent by calling Retell's full
  agent list (paged), and reads the Stripe customer's name, on every view, with no cache. If rate limits bite, cache
  both. Each degrades to a blank name on failure rather than failing the page.
- **A failed nightly run only logs.** There is no alert until Phase 6. Until then, a silently failing Retell audit or
  Twilio pull under-bills with no visible signal.
- **The Stripe webhook is a single point of failure.** If it is unregistered or the signing secret is wrong, every charge
  still succeeds at Stripe while every bill here reads `charging` forever. No reconciliation pass exists; Phase 6 should
  alert on bills stuck in `charging` for N days.
- **`failed` is not final.** Stripe's Smart Retries can later flip a failed invoice to `paid`, and the webhook handles that.
- **Django admin** has `ClientMapping` and `Bill` registered. The GHL tokens and OAuth history cannot be inspected through
  the admin yet.
- **The install check is weaker** (see §4). Any agency that satisfies the "Company" type can install. A private marketplace
  app limits the exposure.
- **Refresh-token locking assumes one database.** It is fine on a single Postgres. Revisit it before running multiple app
  instances.
- **The 48-hour Retell window is a constant**, not a setting. A correction older than 48 hours is never caught.
- **`TWILIO_AUTH_TOKEN` sits in `.env`.** If the server is compromised, Twilio's master credential is exposed. The only
  mitigations are file permissions and the gitignore.
- **`local_folder/` is gitignored.** `plan.md` and `CriticalFindings.md` exist **only on the development machine**. Commit
  `6d0b7b1` claims `plan.md` is in it, and it is not.
- **`apps/dashboards` is empty**, and the `frontend/src/features/*-dashboard` folders hold only placeholders.
- **The admin page is built and tested, but the live path is only partly verified.** The SSO handshake, all backend
  endpoints, and component rendering are covered by tests. The user has checked the call status badge on the deployed
  server. The rates save, and the iframe embedding inside GHL, are not recorded as verified live.

---

## 17. Deployment and live checks

### 17.1 Frontend changes on the server

- Rebuild with `npm run build` in `frontend/` (it runs `tsc -b` first; if that fails, `dist/` is left as it was). Then
  restart the Django process (gunicorn).
- Confirm the new bundle is what is served: a string unique to the change should appear in the built JS under
  `frontend/dist/assets/`, and `dist/index.html` should reference the new hashed file name.
- Confirm the browser loads that file name (DevTools, Network). The page runs inside a GHL iframe, so reload the iframe's
  own URL, not only GHL's outer page. A proxy or CDN in front may also cache `index.html`.
- If a build fails partway, `dist/` can keep files from the previous build. Check the timestamps of the files under
  `dist/assets/` after every build.

### 17.1a SSO bridge in Agency Custom JavaScript

The embedded billing dashboard runs in a sidebar iframe and asks its parent window for the encrypted
GHL session with a `REQUEST_USER_DATA` postMessage. GHL answers this for native Custom Pages, but
not for sidebar menu links, so the bridge intercepts the message in the parent window, calls
`window.exposeSessionDetails(APP_ID)`, and posts the encrypted session back to the iframe.

**The script to paste:**

Go to **GoHighLevel** → **Settings** → **Company** → **Whitelabel** → **Custom JavaScript** and paste
the entire block below, replacing whatever is currently there (including any existing favicon code).

```javascript
<script>
/* ---- Favicon (existing — unchanged) ------------------------------------- */
var faviconURL = 'https://assets.cdn.filesafe.space/mDdwkGzgCnH4CYSQKWtQ/media/6adf8bf0f0438c2e5e0528b9.png' // Upload an image into a location funnel and then use the URL here.
var link = document.querySelector("link[rel*='icon']") || document.createElement('link');
link.type = 'image/x-icon';
link.rel = 'shortcut icon';
link.href = faviconURL
document.getElementsByTagName('head')[0].appendChild(link);

/* ---- SurPro Labs billing dashboard: SSO bridge -------------------------- */
/* The dashboard runs in a sidebar iframe and asks its parent for the GHL     */
/* session. GHL answers that question for marketplace Custom Pages but not    */
/* for sidebar menu links, so we answer it here with the same message names.  */
(function () {
  'use strict';

  var APP_ID = 'PASTE_GHL_APP_ID_HERE';                       // = GHL_APP_ID in backend/.env
  var DASHBOARD_ORIGINS = ['https://dashboard.surprolabs.ai']; // add a staging origin here if needed

  // Custom JS can be evaluated more than once as the app navigates; one listener is enough.
  if (window.__surproSsoBridge) return;
  window.__surproSsoBridge = true;

  // The helper is defined by GHL's own bundle and may not exist the instant this runs.
  // Bounded well under the dashboard's 10s wait so a slow load still gets an answer.
  function helperReady() {
    return new Promise(function (resolve, reject) {
      var waited = 0;
      (function check() {
        if (typeof window.exposeSessionDetails === 'function') return resolve();
        if (waited >= 8000) return reject(new Error('window.exposeSessionDetails is not available'));
        waited += 250;
        setTimeout(check, 250);
      })();
    });
  }

  window.addEventListener('message', function (event) {
    try {
      if (!event || !event.data || event.data.message !== 'REQUEST_USER_DATA') return;
      // The session is a bearer credential, so it goes only to our own page.
      if (DASHBOARD_ORIGINS.indexOf(event.origin) === -1) return;

      var frame = event.source;
      if (!frame) return;
      var origin = event.origin;

      function reply(payload) {
        try {
          frame.postMessage({ message: 'REQUEST_USER_DATA_RESPONSE', payload: payload }, origin);
        } catch (err) {
          console.error('[SurPro SSO] could not reply to the dashboard:', err);
        }
      }

      helperReady()
        .then(function () { return window.exposeSessionDetails(APP_ID); })
        .then(function (session) {
          // Documented to resolve to the encrypted string. Anything else is not a session.
          reply(typeof session === 'string' && session ? session : null);
        })
        .catch(function (err) {
          // Replying null makes the dashboard show its fallback at once instead of
          // sitting on a 10-second timeout.
          console.error('[SurPro SSO] could not read the session:', err);
          reply(null);
        });
    } catch (err) {
      console.error('[SurPro SSO] listener error:', err);
    }
  }, false);
})();
</script>
```

**What needs to change:**

Replace the one placeholder `PASTE_GHL_APP_ID_HERE` with the marketplace app's id (the same string
in `backend/.env` as `GHL_APP_ID`, also the id in the app's marketplace URL). No other edits needed
unless the dashboard is hosted from a second domain, in which case add it to `DASHBOARD_ORIGINS`.

**How it works:**

- The script runs once per page load (the guard `if (window.__surproSsoBridge)` prevents re-registration).
- When the iframe posts `{message: 'REQUEST_USER_DATA'}`, the listener checks the origin against
  `DASHBOARD_ORIGINS` (the session is a bearer credential, so it only goes to our own domain).
- It calls `window.exposeSessionDetails(APP_ID)`, waits up to 8 seconds for the GHL helper to
  load if needed, and posts the encrypted result back to the iframe as
  `{message: 'REQUEST_USER_DATA_RESPONSE', payload: <encrypted>}`.
- If anything fails, it replies with `null`, which makes the dashboard show its fallback
  immediately instead of waiting 10 seconds.
- The favicon block is unchanged and remains functional.

**Troubleshooting:**

If the dashboard shows *"Open this page from inside GoHighLevel to see live billing data"* after
the script is deployed:

1. Open the page inside GHL, open the iframe's own URL in a new tab (check DevTools Network for the
   iframe's `src`). If it loads there, the iframe is running.
2. Open DevTools on the GHL page (where the iframe is embedded). Check the Console for
   `[SurPro SSO]` messages. If none appear, the script is not loaded or the message listener never fired.
3. Check the origin in DevTools. Post a message with `window.parent.postMessage({message: 'REQUEST_USER_DATA'}, '*')` and
   look for a reply event. If there is no reply, the listener is not catching the message.
4. If `window.exposeSessionDetails` exists in the console, call `window.exposeSessionDetails('PASTE_GHL_APP_ID_HERE')`
   directly and check if it resolves to a non-empty string. If not, the app id is wrong or the app is not installed.

### 17.1b Before the card flow can work live (Phase 7)

In this order, because each step is useless without the one before it:

1. **Create both custom fields** on contacts in the main account — the card status and the
   rebilling status — and put their ids in `GHL_CARD_STATUS_CUSTOM_FIELD_ID` and
   `GHL_RETELL_STATUS_CUSTOM_FIELD_ID`. Field ids, not names — the lookup is by id.
2. **Re-authorise the marketplace app**, which now asks for `contacts.write`. The
   `locations/customValues.*` scopes the retry work added are gone, since nothing reads or writes
   a custom value any more. Until the re-auth, every card request 502s and every status write is
   logged as a failure.
3. **Check the first search and write by hand** before trusting the button. `GET
   /contacts/search/duplicate?locationId=<main>&email=<owner>` should answer `{"contact": {...}}` —
   the response shape is not in GHL's published spec, and `set_contact_field()` reads `contact.id`.
   The write is `PUT /contacts/{id}` with `{"customFields": [{"id": …, "fieldValue": "card not
   found"}]}`, `Version: v3`.
4. **Build the workflow**: triggered by that field changing to `card not found`, emailing the card
   form, and ending in a Custom Webhook action to `POST /api/rebilling/webhooks/ghl-card/` with
   `X-Card-Webhook-Token` set to `GHL_CARD_WEBHOOK_TOKEN` and a body carrying `locationId`. The
   **pause/resume workflow** triggers the same way, off the rebilling status field changing to
   `failed` or `active` — the point of moving it off the custom value.
5. **Confirm the card lands on the Stripe customer our `billing_email` match finds.** If the form
   writes to a different Stripe account, the webhook will keep saying `card saved` and the page will
   keep saying no card — the one failure here that looks like a bug and is not.

### 17.2 Migrations on the server

- The server's database may hold columns from the other branch (`feat/ghl-billing-with-stripe-fallback`), such as
  `ghl_invoice_id` and `ghl_invoice_voided_at`, which this branch does not know about.
- **Back up the SQLite database file first.** A SQLite table rebuild, which some migrations trigger, can drop columns the
  code does not know about. The backup is the way back.
- Use plain `python manage.py migrate`. **Never `--fake`.**
- Migration `0006` tolerates a `charged_at` column that already exists (§6.2).

### 17.3 Scheduled work and Stripe setup

- Celery worker and beat must both run for the nightly audit and pull (§6.6). Check they are running.
- `charge_bills` is not scheduled. Run it by hand each month, after the Twilio month-close pull and `calculate_bills`.
- `retry_failed_charges` runs hourly on beat. It makes only the retries that are due. The status it writes needs `GHL_RETELL_STATUS_CUSTOM_FIELD_ID` filled in and the app re-authorised for `contacts.write` — §17.1b, which the card flow needs anyway.
- Stripe's own Smart Retries (dashboard setting) run alongside ours. Turn them off in Stripe so the schedule in this app is the only one.
- The Stripe webhook must be registered for `invoice.paid` and `invoice.payment_failed` (§15).

### 17.4 Diagnosing a page that shows the wrong status

Work outward from the data:

1. Check the database for the call (`bill_id`) and the bill (`status`, `failure_reason`).
2. Check the API directly, from the Django shell, for the same month: the detail's `recent_calls[].bill_status` should
   match the database.
3. Check the served bundle (§17.1). If the API is right and the page is wrong, the browser or a cache is serving old JS.

### 17.5 Retell provisioning for multi-location agencies (50+ GHL sub-accounts)

**Before deploying at scale, verify the multi-location architecture (§15) by running the curl commands there.** Once confirmed, provisioning is scriptable end to end.

**Manual steps — once, per sub-account:**

1. In each GHL sub-account: Settings → Private Integrations → Create new Integration. Scopes: Contacts, Opportunities, Custom Fields, Calendar. Copy the token (shown once); save it.
2. Collect all 50+ tokens and Location IDs in a CSV or similar.

**Scriptable provisioning (with the Retell API):**

3. For each client:
   - `POST /create-app` with `provider`, `type: "crm"`, `tenant_id: <Location ID>`, `auth_config.api_key: <token>`, `name: "<client name>"`. Save the `app_id`.
   - `POST /test-app-auth/{app_id}` to verify the credentials are live (check `connection_status` in response).
   - `POST /create-agent` with the app's integration tools bound to that `app_id`, and capture the returned `agent_id`.
   - Write the `agent_id` to `ClientMapping.retell_agent_ids` for that client via the mapping editor (existing flow).

4. After provisioning: `GET /list-apps?limit=1000` to confirm all connections exist with `connection_status: "connected"`.

**Known limits:**

- **20 apps per provider:** Default limit in Retell. Request a custom higher limit if it blocks your client count.
- **Contact sync is workspace-wide:** Only one CRM connection can drive Retell's native contact sync — which is why each client now has a workspace of their own (`plan.md` Phase 9).
- **No workspace creation API:** Workspaces are dashboard-only and isolated. Since 4 Nov there is **one per client**, each created by hand in Retell's dashboard and connected here by pasting its webhook-badged API key into the client's page (§15, `plan.md` Phase 9).

**What changed in this app's code (4 Nov):** everything about how a call arrives. There is no single key: each client's own workspace key pulls their calls via `/v3/list-calls`, and attribution is the workspace itself rather than an `agent_id` match. See `plan.md` Phase 9.

---

## 18. Decisions and why

These were each chosen on purpose. Revisit them only with new information.

- **Stripe is the charging rail.** GHL's one-off invoice auto-charge did not charge when tested live, and GHL's wallet
  pays the developer monthly, which would finance about 45 days of Retell and Twilio cost. Stripe invoices itemise each
  bill, so the client sees the Retell and Twilio lines.
- **One invoice per bill, idempotency keys derived from the bill.** A crash or a manual re-run cannot charge twice.
- **`customer_id` never creates a Stripe customer.** A cardless customer fails at the card for a reason nobody can act on.
- **Rates start today.** `set_rates()` writes a row dated today. Months that have ended keep their rate, so a rate change
  never moves a past bill.
- **Both rates are required on save.** They share one `RateHistory` row, and a half-request would have to invent the
  other half.
- **"Reset to defaults" fills the fields but does not save.** Saving is the act that makes the defaults the client's
  rates. The page holds no copy of the defaults: `default_rates` comes from the backend.
- **The page remembers what it saved.** A rate saved today does not change the rate shown for a past month, so the page
  would otherwise call a rate it has just saved "unsaved".
- **Per-call status comes from the call's own bill, not the month's.** The run bills what exists when it runs. A call
  made afterwards is on no bill and stays billable.
- **The headline is the bill once there is one.** The estimate stays on screen only until the month has a bill.
- **`failure_reason` is shown whatever the status.** `charge_bills` records a failure without setting `failed`, so reading
  it only on `failed` would hide the charges nobody can act on.
- **Names are read live, with a blank fallback.** The Stripe customer name and Retell agent names do not fail the page if
  their provider is down.
- **The sub-account falls back to the client's own name.** A blank or "Not set" field gives an admin nothing to act on.
- **No `created_at` on `Bill`.** Nothing in the plan needed one.
- **Demo data:** Branwynn is left unbilled in the preview, so its "No Stripe customer found" message stays unique in the
  tests that check it.

---

## 19. Git history

Branch `feat/stripe-billing`, oldest first:

| Commit | Message (abridged) |
|---|---|
| `da0ddb3` | GHL onboarding |
| `9e7f703` | Adding location id in `.env`, removing company id |
| `6d0b7b1` | Phase 1: models, Retell webhook, call matching |
| `44c3d94` | Phase 2: Retell nightly reconciliation audit |
| `d9ab56a` | Unbillable call tracking, Celery daily audit schedule |
| `ea95a0c` | Twilio calls integrated with Retell (Phase 2b) |
| `1d9d280` | Agency admin custom page, white and green theme, demo seed data |
| `ca87e72`, `16af42c`, `76face5`, `f3d815a`, `3534ec1` | Admin UI: temporary GHL restriction removal, date filters, table and layout fixes |
| `4012b52` | Agency scope, unmapped clients handled, demo preview separated |
| `f0ec6cc` | GHL installed-locations uses `Version: v3`; sync decoupled from install |
| `cc8e982` | Sync all agency sub-accounts regardless of app install status |
| `3e383bb` | Refresh clients button, location-created webhook |
| `4b21240` | Client mapping editor for live and preview routes |
| `9a4421a` | Phase 3: calculation engine with `SET_NULL` safeguards |
| `1c43a7b` | Stripe initial integration |
| `495bba2`, `2933d10` | Searchable Retell agent and Twilio subaccount pickers |
| `6d3bdc0` | Stripe-primary invoicing; GHL owner email linked to the client |
| `034689c` | Merge `main` into `feat/stripe-billing` |
| `96bb084` | Migration `0006` safe on a database that already has `charged_at` |
| `20c3579` | Persist client rates; show the actual bill status on the admin page |
| `2fb0170` | Paid and billed status on the recent calls table |
| `aa40299` | Call status badge derived from each call's own bill (**deployed**) |
| `f00c41c` | Human-readable names in the mapping chips and the billing contact |
| `bcd48b9` | Automated retry schedule and the first GHL rebilling-status write |
| `654c927` | Twilio billing paused |
| `a14e0a3` | Phase 7: the card on file, and asking for one |

Uncommitted on top of `a14e0a3`: Phase 8, the rebilling status write and SSO bridge (§0 and §17.1a).

Other branches in the repository: `feat/ghl-billing-with-stripe-fallback` (the GHL one-off invoice attempt, with its own
`0006` and the `ghl_invoice_*` columns), `main`, and a few older working branches (`frontend`, `maping`, `retell`, `stripe`).

---

## 20. Memory and collaboration

- Claude's persistent memory for this project is indexed by `MEMORY.md` and holds notes on: the project overview, a rule
  to avoid over-engineering, the Phase 5 billing architecture, and the two GHL location ids (which one is for onboarding
  and which holds the billing contacts). **The Phase 5 note describes the other branch's decision (GHL invoices with
  Stripe as a fallback). It does not describe this branch, which is Stripe-primary (§18).** Update that note when
  convenient.
- The user pastes multi-part specs and expects each part to be worked through in full, with anything not done called out
  explicitly.
- The user wants short, direct summaries: what changed, what was verified, and what was deliberately left out.

---

## 21. How to start a new session on this project

1. Read this file, then `CLAUDE.md`.
2. If the task touches rebilling, read `local_folder/plan.md` and `local_folder/CriticalFindings.md`, if they exist on the machine.
3. Confirm the baseline. From `backend/`, run `.venv/Scripts/python.exe -m pytest -q` and expect
   **355 passed**. From `frontend/`, run `npm test -- --run` and expect **96 passed**. Also run `ruff check .`,
   `ruff format --check .` and `manage.py makemigrations --check --dry-run`, and in `frontend/`
   `./node_modules/.bin/tsc -b` and `./node_modules/.bin/oxlint src`.
4. Check `git status` and `git log --oneline | head` before changing anything. Expect Phase 8 (§0) to be uncommitted.
5. **Note:** Task 4 Phases 1, 2, 2b, 3, 5, 7, 8 and most of 4 are built. Still open: the unassigned-call mapping UI, the bills
   table, Phase 6 (alerts), and the `send_invoice` path (§16). Tasks 2 and 3 are not started. Don't start any of these
   without an explicit request. Ask first if the request is ambiguous. **Never make live API calls.**
6. Before the first live charge, answer the Stripe account question in §15.
