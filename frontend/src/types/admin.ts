export interface SsoSession {
  user_name: string
  email: string
  role: string
  is_agency_owner: boolean
  company_id: string
  active_location: string | null
  scope: 'agency' | 'location'
}

/** The two per-minute rates on their own: what the rates editor saves, with no date yet. */
export interface RateFields {
  voice_rate_cents: number
  call_rate_cents: number
}

export interface Rates extends RateFields {
  start_date: string
}

export interface UsageSummary {
  ai_minutes: string
  ai_cost_cents: number
  call_minutes: string
  call_cost_cents: number
  has_twilio_usage: boolean
  /** An estimate at the current rate, not a bill. Null when no rate has been set. */
  estimated_bill_cents: number | null
  unbillable_calls: number
  twilio_as_of: string | null
  twilio_stale: boolean
}

export interface ClientSummary {
  location_id: string
  client_name: string
  /** False for a sub-account nobody has set up for billing yet. Then `usage` is null too. */
  mapped: boolean
  active: boolean | null
  billing_mode: string | null
  /** This client has their own Retell workspace key, and it worked on the last check. */
  retell_connected: boolean
  twilio_connected: boolean
  rates: Rates | null
  usage: UsageSummary | null
}

export interface ClientsResponse {
  scope: 'agency' | 'location'
  month: string
  /** Whether Twilio minutes are billed here at all. False while Twilio rebilling is paused -
   * GoHighLevel already rebills those minutes natively, so showing them here would read as a
   * second charge. Everything Twilio is hidden, not deleted: it comes back when this is true. */
  twilio_billing_enabled: boolean
  clients: ClientSummary[]
}

export interface TwilioDay {
  usage_date: string
  category: string
  call_count: number
  usage_minutes: string
  twilio_price_cents: number
  as_of: string | null
}

export interface RecentCall {
  call_id: string
  agent_id: string
  from_number: string
  to_number: string
  direction: string
  started_at: string | null
  duration_ms: number | null
  billable_minutes: string
  retell_cost_cents: number | null
  call_status: string
  billing_status: 'billable' | 'unbillable'
  unbillable_reason: string
  /** The status of the bill this call is on, or null while it is on none - a call made after
   * the month was billed is on no bill of its own. */
  bill_status: BillSummary['status'] | null
}

/** One agent in a client's own Retell workspace. Discovered from their key, never typed. */
export interface RetellAgent {
  agent_id: string
  agent_name: string
}

/**
 * A client's own Retell workspace, or null while nobody has given them a key.
 *
 * The key itself is never here: only its last four characters, which are enough to tell two
 * keys apart and no use to anyone who reads them.
 */
export interface RetellWorkspaceSummary {
  /** What the GHL team called it in Retell. Typed, and only ever shown. */
  workspace_name: string
  key_last4: string
  /** `key_invalid` means the nightly audit could not read the workspace with this key. */
  status: 'active' | 'key_invalid'
  /** When the key last proved it still works, ISO UTC, or null if it never has since saving. */
  last_checked_at: string | null
  /**
   * When a signed webhook last arrived. **Null while the status is active is the one visible
   * symptom of a key that is valid but is not the webhook-badged one** - Retell signs with that
   * one key alone, so any other key tests green here and then fails every signature silently.
   */
  last_webhook_at: string | null
  /** Absolute, built on the server from the route. This is what gets pasted into Retell. */
  webhook_url: string
}

/** What a key test found: the agents in that workspace, for a person to recognise. */
export interface RetellKeyTest {
  agents: RetellAgent[]
}

/** What saving a key answers with. Carries the agents too, so the page need not re-ask. */
export interface RetellWorkspaceSaved extends RetellKeyTest {
  location_id: string
  workspace_name: string
  key_last4: string
}

/** One subaccount under Jerry's Twilio parent account. The parent itself is never listed. */
export interface TwilioSubaccount {
  sid: string
  friendly_name: string
}

/**
 * What an agency admin can set on a client's mapping, and nothing else.
 *
 * Retell agents are not among them any more: they are whatever is in that client's own Retell
 * workspace, discovered from their key. Rates, billing mode and the billing email have their
 * own rules and are not edited here either.
 */
export interface MappingFields {
  twilio_subaccount_sid: string
  phone_numbers: string[]
}

export interface MappingResult extends MappingFields {
  location_id: string
  client_name: string
}

/**
 * The bill for the month being viewed, once the monthly run has calculated one.
 *
 * It replaces the page's own estimate from that point on: it was worked out from the rate in
 * effect on each call's own day, so it is what the client is actually charged, and no later
 * rate change can move it.
 */
export interface BillSummary {
  /** `retrying` is a failed charge the backend will try again: not a permanent failure. */
  status: 'uncharged' | 'charging' | 'retrying' | 'paid' | 'failed'
  total_cents: number
  /** Why the last attempt failed. Carried whatever the status - a charge that never reached
   * Stripe records its reason while leaving the bill `uncharged`. */
  failure_reason: string
  /** Retries made so far, out of `max_retries`. Zero for a bill that has never been retried. */
  retry_count: number
  max_retries: number
  /** When the next retry is due, as an ISO-8601 UTC string. Null when none is pending. */
  next_retry_at: string | null
}

/**
 * Whether the client has a card Stripe would charge, and whether we have asked them for one.
 *
 * `no_card` is what Stripe would *do*, not what it holds: a card attached without being made
 * the invoice default cannot be charged, so it is reported as no card rather than as one that
 * would work. `unknown` means Stripe could not be read - never shown as a problem to chase.
 */
export interface CardStatus {
  status: 'card_on_file' | 'no_card' | 'no_customer' | 'no_billing_email' | 'unknown'
  /** Stripe's own word, lowercase (`visa`). Empty for a card held the legacy way. */
  brand: string
  last4: string
  /** When an admin last pressed "Request card", ISO UTC, or null if nobody ever has. */
  requested_at: string | null
}

export interface ClientDetail {
  month: string
  location_id: string
  client_name: string
  mapped: true
  active: boolean
  billing_mode: string
  /** Whether Twilio minutes are billed here at all - see `ClientsResponse`. While it is false
   * `usage.estimated_bill_cents` is Retell AI minutes only, and the page hides the rest. */
  twilio_billing_enabled: boolean
  /** Who GoHighLevel says owns the sub-account, and whether Stripe has been matched to them.
   * All read-only: the owner comes from GHL and the customer is matched by their email. */
  owner_name: string
  billing_email: string
  stripe_customer_id: string
  /** The Stripe customer's own name - empty when Stripe has none on file, or could not be
   * read just now. Never shown in place of the id; the id is never shown at all. */
  stripe_customer_name: string
  /** Why the last bill could not be charged, when no customer was matched. */
  billing_failure_reason: string
  /** Whether Stripe holds a card this client could be charged with, read on every view. */
  card: CardStatus
  retell_agent_ids: string[]
  /** The same ids as `retell_agent_ids`, in the same order, each carrying the agent's own name
   * read off the client's workspace - empty when Retell has no name for it right now, or when
   * the client has no key to ask with. */
  retell_agents: RetellAgent[]
  /** Null while this client is still without a Retell key of their own. */
  retell_workspace: RetellWorkspaceSummary | null
  phone_numbers: string[]
  twilio_subaccount_sid: string
  rates: Rates | null
  rate_history: Rates[]
  /** What the rates editor's "Reset to defaults" writes back, so the page holds no copy of
   * its own of two numbers the backend owns. */
  default_rates: RateFields
  /** Null until this month has been billed, which is what keeps the estimate on screen. */
  bill: BillSummary | null
  usage: UsageSummary
  twilio_days: TwilioDay[]
  recent_calls: RecentCall[]
}

/**
 * A sub-account's own page, opened before an agency admin has mapped it. There is no usage or
 * rates to show yet - just enough to say so.
 */
export interface UnmappedClientDetail {
  month: string
  location_id: string
  client_name: string
  mapped: false
  /** Carried here too, because the mapping editor on this page is gated on it. */
  twilio_billing_enabled: boolean
}

export type ClientDetailResult = ClientDetail | UnmappedClientDetail
