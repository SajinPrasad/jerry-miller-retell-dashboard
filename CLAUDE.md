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
- **New sub-accounts show up two ways:** the agency admin can click "Refresh clients" on `/admin` (`POST /api/rebilling/admin/clients/sync/`, agency scope only), and GHL's own `LocationCreate` webhook calls `POST /api/ghl/webhooks/location-created/` the moment Jerry creates one - available only to agency-level apps, which is what this one is. Both just call `sync_locations` again; neither reads the location fields off the webhook payload directly, so there's one code path for discovering a sub-account, not two. The webhook verifies `X-GHL-Signature` (Ed25519) against `GHL_WEBHOOK_PUBLIC_KEY` - GHL's own public key, published in its docs and the same for every app, not a per-app secret - and always answers 200/401/400 quickly so GHL doesn't retry; a sync failure is logged and left for the next webhook, manual refresh or install callback to catch.
- **Verify on Jerry's account:** the `Version` header (`GHL_API_VERSION`), the `/oauth/locationToken` path (newer docs say `/oauth/location-token`), `installed-locations` pagination, the white-label install host (`GHL_INSTALL_URL_BASE`), and the `LocationCreate` webhook itself - register it under the marketplace app's Advanced Settings > Webhooks pointing at `/api/ghl/webhooks/location-created/`, and confirm GHL still signs with `X-GHL-Signature` (the legacy `X-WH-Signature`/RSA header, which this app does not implement, was slated for deprecation).

## Agency admin page (GHL Custom Page, built)

The embedded admin interface Jerry opens from GHL's left nav: onboarded clients, their rates and
their voice usage. An agency admin can also set up or edit a client's mapping (Retell agents,
phone numbers, Twilio subaccount) here; a sub-account's own page stays read-only. Editing rates is
the rest of Phase 4 and is not built.

- **How GHL identifies the viewer.** The page runs in a GHL iframe, posts
  `{message: 'REQUEST_USER_DATA'}` to `window.parent`, and gets `REQUEST_USER_DATA_RESPONSE` with
  an encrypted blob in `payload`. `apps/ghl/sso.py` opens it: OpenSSL-compatible AES-256-CBC
  (`Salted__` + 8-byte salt, key/IV from EVP_BytesToKey with MD5) using `GHL_APP_SSO_KEY`, the
  app's **Shared Secret** (Advanced Settings > Auth). No new dependency - `cryptography` and
  `hashlib` already ship. Tests pin the derivation against real `openssl enc` vectors.
- **Scope comes from `activeLocation`, never `type`.** GHL reports `type: "agency"` even inside a
  sub-account, so reading `type` would show one client every other client's numbers. There is no
  `locationId` field. `activeLocation` can also hold the agency's **own** location id (it is a
  real `GHLOAuthToken` row, `AGENCY_LOCATION_ID`) rather than being empty - `SSOContext.scope`
  checks against `AGENCY_LOCATION_ID`, not against "is it set at all", so the agency account
  always gets agency scope and the all-clients view, whether or not that location has its own
  mapping.
- **The blob is the credential.** It is sent on every admin request in `X-GHL-Session` and opened
  per request - no cookie (the iframe is cross-site), no JWT, no session store. The session's
  `companyId` must match the stored agency row, which is a real agency check the install path
  cannot make. 401 unreadable, 403 wrong agency.
- **Endpoints:** `POST /api/ghl/sso/session/` (who is viewing), `GET /api/rebilling/admin/clients/`
  and `GET /api/rebilling/admin/clients/<location_id>/`, both taking `?month=YYYY-MM` (default:
  this month). The list is every **sub-account from Task 1** left-joined to `ClientMapping`, so an
  onboarded-but-unmapped client shows as `mapped: false` with null usage - "nobody set this up" must
  not read as "used nothing". Each row also carries `retell_connected` / `twilio_connected`, which
  is what the list's single status pill says on hover.
- **"Refresh clients"** (`ClientBrowser.tsx`'s clients-card header, agency scope only - a
  sub-account's own page never reaches the list, so it never renders): `POST
  /api/rebilling/admin/clients/sync/`, same `?month=` as the list. Re-runs Task 1's
  `sync_locations` against GHL, then returns the same shape `GET /api/rebilling/admin/clients/`
  does, so `AdminPage.tsx` swaps the table in place - no reload, just the state update.
- **Mapping is the one thing an agency admin can write.** `PUT
  /api/rebilling/admin/clients/<location_id>/mapping/` (agency scope only, 403 for a location
  session even on its own client) takes `twilio_subaccount_sid`, `retell_agent_ids`,
  `phone_numbers` - never `active`, `billing_mode` or rates, which have their own rules.
  `ClientMappingSerializer` (`apps/rebilling/serializers.py`) validates a Twilio SID's shape
  (`AC` + 32 hex) and a phone number's digit count, and de-duplicates both lists; `set_mapping()`
  (`services.py`) creates the row on first save (`client_name` from Task 1's sync, never
  user-typed) or updates only the fields sent, leaving the rest alone. `MappingEditor.tsx` is the
  one editor, reused on an already-mapped client's page (`ClientDetail.tsx`, in place of the
  read-only `dl`) and on an unmapped one (`ClientBrowser.tsx`'s `SetUpMapping`, in place of the
  sub-account-facing `UnmappedNotice`) - `canEdit` (`!isSubAccount`) picks which a given viewer
  gets. `ClientMapping` also has a plain Django admin registration now, as a fallback to the page.
- **The agent and the subaccount are picked, not typed.** `GET /api/rebilling/admin/retell-agents/`
  and `GET /api/rebilling/admin/twilio-subaccounts/` (agency scope only, like the mapping write
  they serve) read Jerry's live Retell and Twilio accounts through the services that already hold
  those credentials - `RetellReconciliationService.list_agents()` (`POST /v2/list-agents`, paged on
  `pagination_key`) and `TwilioUsageService.list_subaccounts()` (`GET /2010-04-01/Accounts.json`,
  paged on `next_page_uri`). Each returns the two fields a picker needs, sorted by name. **The
  Twilio parent account is left out of its list**: a client mapped to it would be billed for every
  other client's minutes. A provider that cannot be reached is a 502, which the dropdown says in
  words. `SearchableSelect.tsx` is the one dropdown, used for both: it fetches **when the panel is
  first opened**, not on page load (most visits to a client's page never touch the mapping, and
  each one would otherwise cost two live provider calls), keeps the list for later opens, retries
  on the next open after a failure, and filters on name *or* id as you type. An agent already on
  the client is marked `Added` and not offerable twice; the Twilio list carries a
  `No Twilio subaccount` row, which is how a client is taken off regular-call billing now that the
  SID is not a text box. **Phone numbers are still typed** - they are the backup match for calls no
  mapped agent covers, and they come off a GHL sub-account, not off Retell.
- **An agency admin opening an unmapped client never calls the 404ing detail endpoint.** Both
  `AdminPage.tsx` and `AdminPreviewPage.tsx` skip the detail fetch whenever the clients list has
  already marked the selected row `mapped: false`, and hand `ClientBrowser` a synthesized
  `{month, location_id, client_name, mapped: false}` instead - the same shape the endpoint itself
  returns for a sub-account's own unmapped page. That is what lets `SetUpMapping` render
  immediately instead of racing (and losing to) the 404 described below.
- **Two pages, two jobs.** The list answers "who is onboarded, and is anyone billing them": client
  name and status, nothing else. Every figure - minutes, rates, estimate, cost, margin - and the
  period filter live on the client's own page, where there is room to show how they were reached.
- **An unmapped sub-account opening its own page gets a friendly state, not a 404.** The detail
  endpoint 404s only for an *agency* session reading a client the list already marked unmapped
  (there is genuinely nothing to show yet, and the status is in the list). For a *location*
  session - the client opening its own page - the same unmapped case is a normal 200:
  `{month, location_id, client_name, mapped: false}`, no usage or rates fields at all. The
  frontend (`ClientBrowser.tsx`) renders that as a plain "not set up yet" card instead of an
  error banner. `ClientDetail`'s full response carries `mapped: true` so the two shapes are a
  discriminated union (`ClientDetailResult` in `types/admin.ts`) rather than guessed apart.
- **Money shown is an estimate**, `minutes x the current rate`, labelled as such in the UI. Phase 3
  applies the rate in effect on each call's day and owns `Bill`/`BillLine`. Don't let the admin
  reads in `services.py` grow into Phase 3.
- **Frontend:** `features/admin/` (route `/admin`, the Custom Page URL), `lib/ghlSession.ts`
  (handshake, 10s timeout, listener removed), `api/admin.ts`. White background, green accent.
  Loading states are skeletons shaped like the content (`Skeleton.tsx`), not text.
- **Period filter** (`MonthPicker.tsx`): on the client's page, opposite the "All clients" back
  button. Arrows to step a month, a list to jump, bounded at the current month (nothing is billed
  in the future) and at the oldest listed month (so the select can never hold a value that is not
  one of its options). It drives the detail - usage, daily Twilio rows, recent calls - and the
  list request behind it, through the endpoints' `?month=`. The page only renders figures whose
  `month` matches the one selected, so a slow response never leaves one period's numbers under
  another period's heading.
- **The call list is written for a reader, not a debugger**: date and time, the customer's number
  (the `from`/`to` that is not the client's own line, chosen by direction), direction, duration,
  what Retell charged, and a status pill - `Billable`, or a few words like `Not connected`, with
  the backend's full sentence on hover. `call_id` and `agent_id` are not shown. Times are the
  stored UTC ones, read off the ISO string rather than through `Date`, because the billing window
  and the Twilio days are UTC too and a re-stamped call could appear under a day in another month.
- **What-if rates** (`ClientDetail.tsx`): the client's rates are editable on the page, in React
  state alone - nothing is sent, nothing is saved, and `ClientDetail` is remounted per client and
  per period so a typed rate cannot follow the viewer into another one. While the rates are the
  stored ones the server's own `estimated_bill_cents` is shown; only once a rate differs is the
  estimate recomputed in the browser, so the headline figure cannot drift a cent from the backend
  on its own. Writing a rate back is still Phase 4's editing UI, and is not built.
- **The standalone preview is its own route, `/admin/preview`, separate from the live `/admin`.**
  `AdminPreviewPage.tsx` renders **bundled sample data** (`demoData.ts`) behind an amber
  "Standalone preview" banner and **never calls the admin API or attempts the GHL handshake at
  all** - it cannot expose a real client's rates or call history, and it cannot be reached by
  accident while testing live inside GoHighLevel. `/admin` itself (`AdminPage.tsx`) is always
  live: if there is no GHL parent to ask, or it never answers, the page shows a short message
  linking to `/admin/preview` instead of silently rendering demo data. A session GHL *did* send
  that the backend refuses (401/403) still shows an error there, because that is a
  misconfiguration, not an unframed page. Both pages share their list/detail rendering through
  `ClientBrowser.tsx`, so they stay visually identical. **The backend was not weakened: it still
  requires a valid session for every admin request.** A mapping edit in the preview
  (`MappingEditor`'s `preview` prop) never reaches `saveMapping` - it writes into `demoData.ts`'s
  own in-memory `previewMappings` map instead, which `resetPreviewMappings()` clears between tests.
  The same prop feeds the two pickers from `demoRetellAgents()` / `demoTwilioSubaccounts()`, so
  previewing never reaches Retell or Twilio either.
- **Local development:** the handshake needs a GHL parent, so `local_folder/make_dev_session.py`
  seals a blob for `frontend/.env.local` (`VITE_DEV_SSO_BLOB`) and
  `local_folder/seed_demo_data.py` fills the local database. Both are offline and gitignored. The
  backend verifies a dev blob exactly like a real one - there is no server-side bypass.

## Voice rebilling (Task 4, Phases 1, 2, 2b, 3 and 5 built)

Phases and open questions live in `local_folder/plan.md`; cross-platform contradictions and safeguards live in `local_folder/CriticalFindings.md`. Phase 1 is the data model and the Retell webhook, Phase 2 the nightly Retell audit, Phase 2b the daily Twilio pull, Phase 3 the monthly calculation (`Bill`/`BillLine`), Phase 5 charging through a GHL invoice with an automated Stripe fallback. Phase 4's writing half (rates, unassigned calls, bills table) and Phase 6 (alerts) are not started.

**Two ingest paths, sharing only `ClientMapping`.** Retell is one row per call, written once on the webhook. Twilio is one row per day, re-written on every pull. Do not unify them.

- **Models** (`apps/rebilling/models.py`): `ClientMapping` (`location_id`, `client_name`, `retell_agent_ids`, `phone_numbers`, `twilio_subaccount_sid`, `billing_mode`, `stripe_customer_id`, `billing_contact_id`, `active`), `RateHistory` (`voice_rate_cents` default 25 = the Retell AI minute, `call_rate_cents` default 5 = the Twilio call minute, `start_date`; a rate change adds a row and splits both rates at the same point), `RetellCall` (keyed on `call_id`, holds `duration_ms`, `retell_cost_cents` = `call_cost.combined_cost`, `billing_status`, `unbillable_reason`), `UnassignedCall` (matched no client; same call fields; never dropped), `TwilioUsageDay` (one client, one GMT day, one category; `unique_together` on all three).
- **Webhook** `POST /api/rebilling/webhooks/retell/`: verifies `X-Retell-Signature` over the raw body with `RETELL_API_KEY` (`retell.lib.verify`; 5-minute replay window), handles `call_ended` only, answers 204 empty. Matching is by `agent_id`, then by phone number (`to_number` inbound, `from_number` outbound, digits-only), else `UnassignedCall`. Idempotent on `call_id` across both tables.
- **Settings:** `RETELL_API_KEY` must be the key with the **webhook badge** (it also verifies signatures). `RETELL_ROUNDING` is `per_second` (default) or `round_up_minute` — **Retell only**; Twilio minutes arrive pre-rounded and must never be rounded again. `RETELL_API_BASE_URL`, `RETELL_REQUEST_TIMEOUT`. `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` must be the **parent account's** (a main-account API key is denied subaccount resources), plus `TWILIO_API_BASE_URL`, `TWILIO_REQUEST_TIMEOUT`.
- **Billability:** every stored call carries `billing_status` (`billable`/`unbillable`, default unbillable) and `unbillable_reason` in plain words, set by `billing_state()` on store and on every re-read. A call is unbillable when its status isn't `ended`, or it has no duration or no start time. Nothing is ever dropped; Phase 3 bills `billable` rows only. Duration is taken from `duration_ms`, else `duration_seconds`, else `call_cost.total_duration_seconds`.
- **Reconciliation** `RetellReconciliationService.reconcile()`: `POST /v3/list-calls` over the last 48 hours (`RECONCILE_WINDOW_HOURS`), paged on `pagination_key`, feeding every call through the same `store_call()`; then `GET /v2/get-call/{id}` for calls in the window that are unbillable or still missing a cost, refreshing duration, cost, status and `ended_at` and re-running `billing_state()`. A failed list ends the run (the next window re-covers it); a failed get-call is skipped.
- **Twilio pull** `TwilioUsageService.pull(start_date, end_date)`: for each `active` client with a `twilio_subaccount_sid`, `GET /2010-04-01/Accounts/{sid}/Usage/Records/Daily.json` with `Category=calls`, `IncludeSubaccounts=false` (its default is **true**) and `PageSize=1000`, paging on the `next_page_uri` Twilio hands back. Stored with `update_or_create` on `(client, usage_date, category)` because Twilio keeps revising a day after it ends. Defaults to a rolling 3-day window; pass both dates for the month-close run. Only `calls` is billed — it excludes SIP and trunking calls by Twilio's definition, which is where Retell's minutes land. A failed client is logged, counted in `failed` and skipped; the others still land.
- **Schedule:** Celery beat runs `apps.rebilling.tasks.reconcile_retell_calls` at **03:00 UTC** and `apps.rebilling.tasks.pull_twilio_usage` at **03:30 UTC** (`CELERY_BEAT_SCHEDULE` in `config/settings/base.py`); both need a worker and a beat process. Each has a management command that does the same work synchronously for a manual run: `manage.py reconcile_retell_calls` and `manage.py pull_twilio_usage [--start YYYY-MM-DD --end YYYY-MM-DD]`. The Twilio command exits non-zero only when **every** client failed (systemic: credentials, Twilio down); a partial failure warns and exits 0.
- **Charging (Phase 5) has two rails and one entry point.** `apps/rebilling/dual_rail.py`'s
  `charge_bills(month)` is the only thing that may start a charge; `ghl_billing.py` and
  `stripe_billing.py` each only know how to work one provider. Command:
  `manage.py charge_bills [--month YYYY-MM]` - deliberately **not** on the Celery schedule, and
  safe to re-run, because re-running is how the fallback happens at all.
- **The primary rail is a GHL invoice** (`apps/rebilling/ghl_billing.py`, `httpx` like the ingest
  paths). Per `uncharged` `Bill`: read the contact from GHL, `POST /invoices/`, save
  `ghl_invoice_id` **before** sending so a half-failure is still traceable, then
  `POST /invoices/{id}/send` with `autoPayment: {enable: true}` - which with no `paymentMethodId`
  is what charges the card already on file, and there is no public endpoint that reads a saved
  card so there is no id to send anyway. `Bill` moves to `charging` and `charged_at` starts the
  fallback clock.
  - **Invoices are raised in `GHL_MAIN_LOCATION_ID`, never `AGENCY_LOCATION_ID`.** Order forms,
    contacts and saved cards are sub-account scoped; the agency account holds none of them, so an
    invoice pointed there silently finds nobody to bill. The existing `get_location_token()`
    mints the token.
  - **`items[].amount` is in dollars, not cents** - GHL types it as a number where Stripe's is an
    integer. The division lives in `_item()` and nowhere else; `bill_cents` stays Phase 3's
    integer and no money is ever held as a float. *Unverified against a real invoice.*
  - The contact's **name, email and phone are read from GHL per invoice**, so they cannot drift;
    a contact we cannot read stops the invoice rather than guessing. A missing phone bills as
    `N/A`. The email is written back to `ClientMapping.billing_email`, which is the only thing
    that carries across to the Stripe rail.
- **The fallback is a timer, not a reaction, because GHL publishes no charge-failed webhook.** A
  declined invoice and one GHL accepted and quietly did nothing with are indistinguishable from
  here - both are a `charging` bill that never becomes `paid`, and the silent no-op is the
  dangerous one because it reads as success in every log. So `_fall_back_to_stripe()` sweeps
  `charging` bills older than `BILLING_FALLBACK_WINDOW_HOURS` **across every month**, not just
  the one being charged.
  - **72 hours is a floor, not a preference.** GHL retries a decline twice more, 24h apart, and
    that is not configurable. Falling back sooner races GHL's own retry and bills twice.
  - **The GHL invoice is voided before Stripe is touched, always.** A void that fails withholds
    the fallback entirely (`fallback_blocked`) and waits for a human: a bill nobody charged can
    be charged tomorrow, a bill charged on both rails has taken the money twice.
- **The fallback rail is Stripe** (`apps/rebilling/stripe_billing.py`, the **official SDK**, not
  hand-rolled `httpx`, because this is money movement). `charge_on_stripe(bill)` looks the
  customer up by `ClientMapping.billing_email` before ever creating one, creates the invoice with
  `collection_method='charge_automatically'` and `auto_advance=True`, one `InvoiceItem` per
  `BillLine` whose `amount` **is** `bill_cents` (no conversion), then `finalize_invoice`, which is
  what makes Stripe attempt the card. Every write carries an idempotency key derived from the bill
  (`bill-<pk>-invoice`/`-retell`/`-twilio`/`-finalize`). `setup_link()` returns a Checkout session
  in `mode='setup'` for a client with no card; nothing calls it from a UI yet.
  - `stripe_billing.charge_bills(month)` is kept as a deliberate escape hatch, reachable only
    through `manage.py charge_bills --stripe-only --i-understand-this-skips-ghl` - **two flags,
    not one**, so a typo, a copied command, or cron inheriting old arguments cannot trigger it.
    Nothing on the Celery schedule ever passes either flag. It only ever touches `uncharged`
    bills, so it cannot collide with one GHL already has an invoice out for (that bill is already
    `charging`).
- **`Bill` carries both rails:** `status` (`uncharged`/`charging`/`paid`/`failed`),
  `ghl_invoice_id`, `ghl_invoice_voided_at`, `charged_at`, `stripe_invoice_id`, `paid_at`. There
  is no status saying which rail a `charging` bill is on, because the two invoice ids already
  answer that, and none saying "GHL declined" because GHL never tells us. A client whose
  `billing_mode` is not `auto_charge` is skipped on **both** rails, never charged.
  Settings: `GHL_MAIN_LOCATION_ID`, `GHL_INVOICE_LIVE_MODE` (deliberately not DEBUG's shadow, so
  staging can be live and production can dry-run), `BILLING_FALLBACK_WINDOW_HOURS`,
  `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` (the last two are different secrets).
- **GHL invoice webhook** `POST /api/rebilling/webhooks/ghl-invoices/`: **`InvoicePaid` only**.
  GHL sends six other invoice events and not one of them means the charge failed, because no such
  event exists - that gap is what the fallback window covers. Signature verified over the raw body
  with `apps/ghl/webhooks.py`'s `verify_signature`, the same Ed25519 check `LocationCreate` uses
  (moved out of `ghl/views.py` once there were two callers, so the two cannot drift). 401 bad
  signature, 400 unreadable body, 204 otherwise. `paid_at` comes from GHL's `paidAt`, not `now()`.
  An event for an invoice we did not raise is ignored, not an error: that sub-account invoices
  clients for their plans too.
- **Stripe webhook** `POST /api/rebilling/webhooks/stripe/`: `invoice.paid` and
  `invoice.payment_failed` only, signature verified over the raw body with
  `stripe.Webhook.construct_event` before anything parses it; 401 bad signature, 400 unreadable
  body, 204 empty otherwise. `paid_at` comes from the event's own timestamp, not `now()`, because
  a webhook can be retried hours later. **The webhook is the only thing that moves a bill off
  `charging`** - if it is misconfigured, charges succeed at Stripe while every bill here still
  reads `charging`. Note the SDK hands `event['data']['object']` back as a `stripe.Invoice`, not a
  dict, so `.get()` raises rather than returning `None`.
- **Not built yet:** no alerts beyond a log line. Nothing reads `TwilioUsageDay` or `call_rate_cents` yet — Phase 3 does that. A mapping is set up or edited from the agency admin page (see above) or, as a fallback, the Django admin; rates still come from the shell.
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
