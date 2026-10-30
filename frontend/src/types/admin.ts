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
  /** Retell agents or phone numbers are mapped, so Retell calls can reach this client. */
  retell_connected: boolean
  twilio_connected: boolean
  rates: Rates | null
  usage: UsageSummary | null
}

export interface ClientsResponse {
  scope: 'agency' | 'location'
  month: string
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
}

/** One agent on Jerry's Retell account, as the mapping editor's picker lists it. */
export interface RetellAgent {
  agent_id: string
  agent_name: string
}

/** One subaccount under Jerry's Twilio parent account. The parent itself is never listed. */
export interface TwilioSubaccount {
  sid: string
  friendly_name: string
}

/** What an agency admin can set on a client's mapping, and nothing else - rates, billing mode
 * and the billing email have their own rules and are not edited here. */
export interface MappingFields {
  twilio_subaccount_sid: string
  retell_agent_ids: string[]
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
  status: 'uncharged' | 'charging' | 'paid' | 'failed'
  total_cents: number
  /** Why the last attempt failed. Carried whatever the status - a charge that never reached
   * Stripe records its reason while leaving the bill `uncharged`. */
  failure_reason: string
}

export interface ClientDetail {
  month: string
  location_id: string
  client_name: string
  mapped: true
  active: boolean
  billing_mode: string
  /** Who GoHighLevel says owns the sub-account, and whether Stripe has been matched to them.
   * All read-only: the owner comes from GHL and the customer is matched by their email. */
  owner_name: string
  billing_email: string
  stripe_customer_id: string
  /** Why the last bill could not be charged, when no customer was matched. */
  billing_failure_reason: string
  retell_agent_ids: string[]
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
}

export type ClientDetailResult = ClientDetail | UnmappedClientDetail
