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
4. **Retell voice rebilling** – per Retell call: get details (duration, agent/number) → match to sub-account by agent or phone number → store minutes per client → at period end minutes × per-client rate → charge → totals feed both dashboards. Rate is a per-client setting (works for any client, e.g. Branwynn). Charging: GHL has no wallet API; test invoice with auto-payment via GHL invoices API first, Stripe as backup. Verify in Retell docs whether a post-call webhook exists and which fields it sends. Open: monthly vs prepaid billing, Retell API access.

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

Agency admin opens `/onboarding` (React) -> "Connect to GoHighLevel" -> GHL install screen -> GHL redirects to `GET /api/ghl/oauth/callback/` -> backend exchanges the code for the agency token (`user_type=Company`; rejected unless `companyId == GHL_COMPANY_ID`), then syncs sub-accounts, then redirects to `FRONTEND_URL/onboarding?status=connected` (or `?error=install_cancelled|install_failed`).

- **Model** `GHLOAuthToken` (`apps/ghl/models.py`), fixed fields: `company_id`, `location_id` (blank on the agency row), `name`, `is_agency`, `is_active`, `access_token`, `refresh_token` (agency row only), `expires_at`. Tokens are Fernet-encrypted (`FIELD_ENCRYPTION_KEY`).
- **Service** `GHLOAuthService` (`apps/ghl/services.py`): `install_url`, `exchange_code`, `get_agency`, `get_agency_token` (refreshes within 5 min of expiry; refresh tokens are single-use, so it locks the row), `get_location_token(location_id)` (re-mints from the agency token when expiring), `sync_locations` (`GET /oauth/installed-locations`, then `POST /oauth/locationToken` per new or expired sub-account; deactivates sub-accounts no longer listed).
- **Endpoints:** `GET /api/ghl/onboarding/` (install link, agency, sub-accounts; no tokens), `POST /api/ghl/onboarding/sync/`, `GET /api/ghl/oauth/callback/`.
- **Marketplace app config:** redirect URI = `GHL_REDIRECT_URI`, scopes = `GHL_SCOPES`, target user = Agency, auto-install on future sub-accounts.
- **Verify on Jerry's account:** the `Version` header (`GHL_API_VERSION`), the `/oauth/locationToken` path (newer docs say `/oauth/location-token`), `installed-locations` pagination, and the white-label install host (`GHL_INSTALL_URL_BASE`).

## Structure

```
backend/                Django 5.2 + DRF
  config/settings/      base / development / production / test
  apps/ghl/             Task 1: GHL onboarding (OAuth install, agency + sub-account tokens)
  apps/common/          EncryptedTextField for secrets
  apps/dashboards/      Tasks 2-3: KPI aggregation + settings endpoints
  apps/rebilling/       Task 4: Retell webhook, usage store, billing
  requirements/         *.in (edit) -> *.txt (pip-compile output, committed)
frontend/               React + TypeScript (Vite); src/{api,components,features,hooks,lib,types}
                        features/onboarding = GHL connect page (route /onboarding)
local_folder/           Untracked local files (gitignored); PDFs in local_folder/pdfs/
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
