# SurPro Labs AI – Backend Tasks

Jerry Miller runs SurPro Labs AI: white-label GoHighLevel (GHL) CRM for HVAC companies ($397/mo + $997 setup); also non-HVAC clients (e.g. Branwynn Destinations). The GHL team builds everything inside GHL. **Our scope is what GHL can't do:** dashboards reading across all client sub-accounts, and billing for Retell voice AI. Example client in the docs: Cool Breeze HVAC.

Source docs live in `local_folder/pdfs/` (untracked): the backend tasks PDF (our scope), the agency snapshot plan (KPI definitions), and the Branwynn scope of work (context only).

## Tasks

| # | Task | Due | Priority |
|---|------|-----|----------|
| 1 | Marketplace app | Oct 23 | High |
| 2 | Agency dashboard | Oct 30 (ready parts) | High |
| 3 | Client dashboard | Nov 4 | Medium |
| 4 | Retell voice rebilling | Nov 13 | High |

1. **Marketplace app** – private GHL marketplace app, installed at agency level, auto-installed on every sub-account. Exchanges the agency token for a per-location token (agency token alone can't read sub-account contacts/opportunities). One custom menu link loads the React app: agency view shows all clients, sub-account view shows only that client (viewer/location come from the context GHL passes on page open). Scopes limited to locations, contacts, opportunities, calendars, conversations, SaaS. Test SaaS endpoints on Jerry's account first (subscription response format undocumented).
2. **Agency dashboard** – MRR / live clients / clients in onboarding (SaaS API; real data only once Stripe + SaaS mode are on); leads, calls booked, clients signed by source (contacts/opportunities/calendars); clients at risk (lead-drop rules editable in settings, e.g. leads down 50% vs previous 30 days; NO login data, API lacks it); voice usage revenue (placeholder until Task 4). **Do not build** "builds finished within 10 business days" yet – only plan for it: start/end points (form, stage or field) must be settings.
3. **Client dashboard** – same page scoped to one location: leads, booked jobs, response time (from conversation message times; verify data exists first), voice usage (after Task 4). Reviews and revenue by source are ON HOLD (no reviews API, source unconfirmed).
4. **Retell voice rebilling** – split Retell's single bill per client (per sub-account), apply Jerry's per-client AI-minute rate, bill each client separately from their $397 plan. **Twilio is out of scope for now** (GHL may rebill SMS/calls natively). Phased plan in `local_folder/plan.md`; source PDF `local_folder/pdfs/SurPro Labs AI Rebilling (Retell and Twilio).pdf`.

## Engineering rules (read before building anything)

The user has had to strip out over-engineering before. Build the simplest thing that meets the stated requirement.

- **Build only what is asked.** No extra endpoints, fields, settings, models, webhooks, permissions or UI controls "for later". If something seems missing, ask or list it as a suggestion in the summary. Don't build it.
- **Data models are decided by the user.** Use exactly the fields given. Don't add audit timestamps, status enums, JSON blobs, extra flags or helper tables without asking.
- **Follow the reference projects' shape** (`D:\tasks\Longevity\`, `D:\tasks\Proinsight projects\product_updates_automation\`). Use the official GHL docs for API facts, not for architecture.
- **One code path.** No fallbacks for hypothetical API variants, no "try A then B". Pick the documented option, make it a setting only if it really varies per environment, and note open questions in this file.
- **No speculative abstractions.** No custom QuerySets/managers, base classes, mixins or helper modules until there are at least two real users. Prefer a plain `.filter()`.
- **Keep the error handling small.** Use one exception per real caller distinction. Callers show a short user-facing reason, not a code for every failure.
- **Security:** keep secrets encrypted and out of URLs and responses. Don't add extra auth layers, signatures or tokens that weren't requested. Mention the risk in the summary instead.
- **Tests cover the behaviour that was asked for.** Don't write tests for features that weren't requested.
- When in doubt, do less and say what you left out.

## GHL onboarding (Task 1, built)

Agency admin opens `/onboarding` (React) -> "Connect to GoHighLevel" -> GHL install screen -> GHL redirects to `GET /api/oauth/callback/` -> backend exchanges the code for the agency token (`user_type=Company`; rejected if installed on a sub-account; the agency row stores `AGENCY_LOCATION_ID` as its `location_id`, and sync skips that location), then syncs sub-accounts, then redirects to `FRONTEND_URL/onboarding?status=connected` (or `?error=install_cancelled|install_failed`).

- **Model** `GHLOAuthToken` (`apps/ghl/models.py`), fixed fields: `company_id`, `location_id` (blank on the agency row), `name`, `is_agency`, `is_active`, `access_token`, `refresh_token` (agency row only), `expires_at`. Tokens are Fernet-encrypted (`FIELD_ENCRYPTION_KEY`).
- **Service** `GHLOAuthService` (`apps/ghl/services.py`): `install_url`, `exchange_code`, `get_agency`, `get_agency_token` (refreshes within 5 min of expiry; refresh tokens are single-use, so it locks the row), `get_location_token(location_id)` (re-mints from the agency token when expiring), `sync_locations` (`GET /oauth/installed-locations`, then `POST /oauth/locationToken` per new or expired sub-account; deactivates sub-accounts no longer listed).
- **Endpoints:** `GET /api/ghl/onboarding/` (install link, agency, sub-accounts; no tokens), `POST /api/ghl/onboarding/sync/`, `GET /api/oauth/callback/`.
- **Marketplace app config:** redirect URI = `GHL_REDIRECT_URI`, scopes = `GHL_SCOPES`, target user = Agency, auto-install on future sub-accounts. The Custom Page needs the **Shared Secret** (`GHL_APP_SSO_KEY`); SSO is not an OAuth scope, so `GHL_SCOPES` is unchanged.
- **Verify on Jerry's account:** the `Version` header (`GHL_API_VERSION`), the `/oauth/locationToken` path (newer docs say `/oauth/location-token`), `installed-locations` pagination, and the white-label install host (`GHL_INSTALL_URL_BASE`).

## Agency admin page (GHL Custom Page, read-only, built)

The embedded admin interface Jerry opens from GHL's left nav: onboarded clients, their rates and
their voice usage. Read-only - editing rates/mapping is the rest of Phase 4 and is not built.

- **How GHL identifies the viewer.** The page runs in a GHL iframe, posts
  `{message: 'REQUEST_USER_DATA'}` to `window.parent`, and gets `REQUEST_USER_DATA_RESPONSE` with
  an encrypted blob in `payload`. `apps/ghl/sso.py` opens it: OpenSSL-compatible AES-256-CBC
  (`Salted__` + 8-byte salt, key/IV from EVP_BytesToKey with MD5) using `GHL_APP_SSO_KEY`, the
  app's **Shared Secret** (Advanced Settings > Auth). No new dependency - `cryptography` and
  `hashlib` already ship. Tests pin the derivation against real `openssl enc` vectors.
- **Scope comes from `activeLocation`, never `type`.** GHL reports `type: "agency"` even inside a
  sub-account, so reading `type` would show one client every other client's numbers. There is no
  `locationId` field.
- **The blob is the credential.** It is sent on every admin request in `X-GHL-Session` and opened
  per request - no cookie (the iframe is cross-site), no JWT, no session store. The session's
  `companyId` must match the stored agency row, which is a real agency check the install path
  cannot make. 401 unreadable, 403 wrong agency.
- **Endpoints:** `POST /api/ghl/sso/session/` (who is viewing), `GET /api/rebilling/admin/clients/`
  and `GET /api/rebilling/admin/clients/<location_id>/`, both taking `?month=YYYY-MM` (default:
  this month). The list is every **sub-account from Task 1** left-joined to `ClientMapping`, so an
  onboarded-but-unmapped client shows as `mapped: false` with null usage - "nobody set this up" must
  not read as "used nothing".
- **Money shown is an estimate**, `minutes x the current rate`, labelled as such in the UI. Phase 3
  applies the rate in effect on each call's day and owns `Bill`/`BillLine`. Don't let the admin
  reads in `services.py` grow into Phase 3.
- **Frontend:** `features/admin/` (route `/admin`, the Custom Page URL), `lib/ghlSession.ts`
  (handshake, 10s timeout, listener removed), `api/admin.ts`. White background, green accent.
  Loading states are skeletons shaped like the content (`Skeleton.tsx`), not text.
- **Period filter** (`MonthPicker.tsx`): arrows to step a month, a list to jump, bounded at the
  current month (nothing is billed in the future) and at the oldest listed month (so the select
  can never hold a value that is not one of its options). It drives both the list and the open
  client's detail, including its daily Twilio rows and recent calls, through the endpoints'
  `?month=`. The page only renders figures whose `month` matches the one selected, so a slow
  response never leaves one period's numbers under another period's heading.
- **Standalone preview (temporary).** Opened outside GHL - directly on an IP, before the domain
  and Custom Page exist - the page does not dead-end. It renders **bundled sample data**
  (`demoData.ts`) behind an amber "Standalone preview" banner, and **never calls the admin API**,
  so previewing cannot expose a real client's rates or call history. A real GHL session always
  takes precedence; `window.parent === window` is what distinguishes "not framed" from "framed
  but unanswered". A session GHL *did* send that the backend refuses (401/403) still shows an
  error, because that is a misconfiguration and samples would hide it. **The backend was not
  weakened: it still requires a valid session for every admin request.** Delete `demoData.ts`
  and the preview branch in `AdminPage` once the page is reached through GHL for good.
- **Local development:** the handshake needs a GHL parent, so `local_folder/make_dev_session.py`
  seals a blob for `frontend/.env.local` (`VITE_DEV_SSO_BLOB`) and
  `local_folder/seed_demo_data.py` fills the local database. Both are offline and gitignored. The
  backend verifies a dev blob exactly like a real one - there is no server-side bypass.

## Voice rebilling (Task 4, Phases 1, 2 and 2b built)

Phases and open questions live in `local_folder/plan.md`; cross-platform contradictions and safeguards live in `local_folder/CriticalFindings.md`. Phase 1 is the data model and the Retell webhook, Phase 2 the nightly Retell audit, Phase 2b the daily Twilio pull; Phases 3-6 (calculation, admin page, charging, alerts) are not started.

**Two ingest paths, sharing only `ClientMapping`.** Retell is one row per call, written once on the webhook. Twilio is one row per day, re-written on every pull. Do not unify them.

- **Models** (`apps/rebilling/models.py`): `ClientMapping` (`location_id`, `client_name`, `retell_agent_ids`, `phone_numbers`, `twilio_subaccount_sid`, `billing_mode`, `stripe_customer_id`, `billing_contact_id`, `active`), `RateHistory` (`voice_rate_cents` default 25 = the Retell AI minute, `call_rate_cents` default 5 = the Twilio call minute, `start_date`; a rate change adds a row and splits both rates at the same point), `RetellCall` (keyed on `call_id`, holds `duration_ms`, `retell_cost_cents` = `call_cost.combined_cost`, `billing_status`, `unbillable_reason`), `UnassignedCall` (matched no client; same call fields; never dropped), `TwilioUsageDay` (one client, one GMT day, one category; `unique_together` on all three).
- **Webhook** `POST /api/rebilling/webhooks/retell/`: verifies `X-Retell-Signature` over the raw body with `RETELL_API_KEY` (`retell.lib.verify`; 5-minute replay window), handles `call_ended` only, answers 204 empty. Matching is by `agent_id`, then by phone number (`to_number` inbound, `from_number` outbound, digits-only), else `UnassignedCall`. Idempotent on `call_id` across both tables.
- **Settings:** `RETELL_API_KEY` must be the key with the **webhook badge** (it also verifies signatures). `RETELL_ROUNDING` is `per_second` (default) or `round_up_minute` — **Retell only**; Twilio minutes arrive pre-rounded and must never be rounded again. `RETELL_API_BASE_URL`, `RETELL_REQUEST_TIMEOUT`. `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` must be the **parent account's** (a main-account API key is denied subaccount resources), plus `TWILIO_API_BASE_URL`, `TWILIO_REQUEST_TIMEOUT`.
- **Billability:** every stored call carries `billing_status` (`billable`/`unbillable`, default unbillable) and `unbillable_reason` in plain words, set by `billing_state()` on store and on every re-read. A call is unbillable when its status isn't `ended`, or it has no duration or no start time. Nothing is ever dropped; Phase 3 bills `billable` rows only. Duration is taken from `duration_ms`, else `duration_seconds`, else `call_cost.total_duration_seconds`.
- **Reconciliation** `RetellReconciliationService.reconcile()`: `POST /v3/list-calls` over the last 48 hours (`RECONCILE_WINDOW_HOURS`), paged on `pagination_key`, feeding every call through the same `store_call()`; then `GET /v2/get-call/{id}` for calls in the window that are unbillable or still missing a cost, refreshing duration, cost, status and `ended_at` and re-running `billing_state()`. A failed list ends the run (the next window re-covers it); a failed get-call is skipped.
- **Twilio pull** `TwilioUsageService.pull(start_date, end_date)`: for each `active` client with a `twilio_subaccount_sid`, `GET /2010-04-01/Accounts/{sid}/Usage/Records/Daily.json` with `Category=calls`, `IncludeSubaccounts=false` (its default is **true**) and `PageSize=1000`, paging on the `next_page_uri` Twilio hands back. Stored with `update_or_create` on `(client, usage_date, category)` because Twilio keeps revising a day after it ends. Defaults to a rolling 3-day window; pass both dates for the month-close run. Only `calls` is billed — it excludes SIP and trunking calls by Twilio's definition, which is where Retell's minutes land. A failed client is logged, counted in `failed` and skipped; the others still land.
- **Schedule:** Celery beat runs `apps.rebilling.tasks.reconcile_retell_calls` at **03:00 UTC** and `apps.rebilling.tasks.pull_twilio_usage` at **03:30 UTC** (`CELERY_BEAT_SCHEDULE` in `config/settings/base.py`); both need a worker and a beat process. Each has a management command that does the same work synchronously for a manual run: `manage.py reconcile_retell_calls` and `manage.py pull_twilio_usage [--start YYYY-MM-DD --end YYYY-MM-DD]`. The Twilio command exits non-zero only when **every** client failed (systemic: credentials, Twilio down); a partial failure warns and exits 0.
- **Not built yet:** no Django admin registration, so mappings are created from the shell; no alerts beyond a log line; no bills. Nothing reads `TwilioUsageDay` or `call_rate_cents` yet — Phase 3 does that.
- **Verify on the first real call:** whether `call_ended` carries `duration_ms` and `call_cost` (the docs' own definition implies yes; the sample payload doesn't show it), and that no agent overrides the account-level webhook URL. For Twilio: that Retell minutes appear only under the `trunking-*` categories and never under `calls`. **Nothing here has run against a live Twilio or Retell account** — every test mocks both.

## Structure

```
backend/                Django 5.2 + DRF
  config/settings/      base / development / production / test
  config/celery.py      Celery app; scheduled jobs in settings CELERY_BEAT_SCHEDULE
  apps/ghl/             Task 1: GHL onboarding (OAuth install, agency + sub-account tokens)
  apps/common/          EncryptedTextField for secrets
  apps/dashboards/      Tasks 2-3: KPI aggregation + settings endpoints
  apps/rebilling/       Task 4: Retell webhook + audit, Twilio daily pull, rates (Ph. 1, 2, 2b)
  requirements/         *.in (edit) -> *.txt (pip-compile output, committed)
frontend/               React + TypeScript (Vite); src/{api,components,features,hooks,lib,types}
                        features/onboarding = GHL connect page (route /onboarding)
                        features/admin      = GHL Custom Page (route /admin)
local_folder/           Untracked local files (gitignored); PDFs in pdfs/, Task 4 plan in plan.md
```

Conventions: business logic in each app's `services.py` (views stay thin); DRF serializers + APIViews; config via env vars (`backend/.env`, see `.env.example`); API under `/api/<app>/`.

## Commands

Backend (run from `backend/`; venv at `backend/.venv`, activate with `.venv/Scripts/activate` on Windows):
```
python -m venv .venv && pip install pip-tools     # first-time setup
pip-sync requirements/dev.txt                     # install deps
pip-compile requirements/base.in -o requirements/base.txt   # after editing a .in file
pip-compile requirements/dev.in -o requirements/dev.txt
pip-compile requirements/prod.in -o requirements/prod.txt
cp .env.example .env                              # then fill in values
python manage.py migrate && python manage.py runserver   # http://localhost:8000
celery -A config worker -l info                   # background jobs (needs REDIS_URL up)
celery -A config beat -l info                     # scheduled (Retell 03:00, Twilio 03:30 UTC)
pytest                                            # tests (config.settings.test)
ruff check . && ruff format .                     # lint / format
```
Use `pip-tools` (never plain `pip install <pkg>` for project deps). Add deps to `requirements/*.in`, recompile, then `pip-sync`.

Frontend (run from `frontend/`):
```
npm install
npm run dev      # http://localhost:5173, proxies /api -> :8000
npm run build
npm run lint
npm test         # vitest
```

## Notes
- Production sets `X_FRAME_OPTIONS = 'ALLOWALL'` because the app is embedded in GHL; tighten to GHL origins when known.
- Keep this file updated when structure, commands or task status change.
