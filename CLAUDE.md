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
- **Marketplace app config:** redirect URI = `GHL_REDIRECT_URI`, scopes = `GHL_SCOPES`, target user = Agency, auto-install on future sub-accounts.
- **Verify on Jerry's account:** the `Version` header (`GHL_API_VERSION`), the `/oauth/locationToken` path (newer docs say `/oauth/location-token`), `installed-locations` pagination, and the white-label install host (`GHL_INSTALL_URL_BASE`).

## Retell rebilling (Task 4, Phases 1-2 built)

Phases and open questions live in `local_folder/plan.md`. Phase 1 is the data model and the webhook, Phase 2 the nightly audit; Phases 3-6 (calculation, admin page, charging, alerts) are not started.

- **Models** (`apps/rebilling/models.py`): `ClientMapping` (`location_id`, `client_name`, `retell_agent_ids`, `phone_numbers`, `billing_mode`, `stripe_customer_id`, `billing_contact_id`, `active`), `RateHistory` (`voice_rate_cents` default 25, `start_date`; a rate change adds a row), `RetellCall` (keyed on `call_id`, holds `duration_ms` and `retell_cost_cents` = `call_cost.combined_cost`), `UnassignedCall` (matched no client; never dropped).
- **Webhook** `POST /api/rebilling/webhooks/retell/`: verifies `X-Retell-Signature` over the raw body with `RETELL_API_KEY` (`retell.lib.verify`; 5-minute replay window), handles `call_ended` only, answers 204 empty. Matching is by `agent_id`, then by phone number (`to_number` inbound, `from_number` outbound, digits-only), else `UnassignedCall`. Idempotent on `call_id` across both tables.
- **Settings:** `RETELL_API_KEY` must be the key with the **webhook badge** (it also verifies signatures). `RETELL_ROUNDING` is `per_second` (default) or `round_up_minute`. `RETELL_API_BASE_URL`, `RETELL_REQUEST_TIMEOUT`.
- **Reconciliation** `RetellReconciliationService.reconcile()`: `POST /v3/list-calls` over the last 48 hours (`RECONCILE_WINDOW_HOURS`), paged on `pagination_key`, feeding every call through the same `store_call()`; then `GET /v2/get-call/{id}` to fill `duration_ms`/`retell_cost_cents` for calls in the window still missing them. A failed list ends the run (the next window re-covers it); a failed get-call is skipped.
- **Command:** `python manage.py reconcile_retell_calls`, meant for nightly cron. Nothing schedules it yet.
- **Not built yet:** no Django admin registration, so mappings are created from the shell; no alerts beyond a log line; no bills.
- **Verify on the first real call:** whether `call_ended` carries `duration_ms` and `call_cost` (docs don't say), and that no agent overrides the account-level webhook URL.

## Structure

```
backend/                Django 5.2 + DRF
  config/settings/      base / development / production / test
  apps/ghl/             Task 1: GHL onboarding (OAuth install, agency + sub-account tokens)
  apps/common/          EncryptedTextField for secrets
  apps/dashboards/      Tasks 2-3: KPI aggregation + settings endpoints
  apps/rebilling/       Task 4: Retell webhook, call store, rates, nightly audit (Phases 1-2)
  requirements/         *.in (edit) -> *.txt (pip-compile output, committed)
frontend/               React + TypeScript (Vite); src/{api,components,features,hooks,lib,types}
                        features/onboarding = GHL connect page (route /onboarding)
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
