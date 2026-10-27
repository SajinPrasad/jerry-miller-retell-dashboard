export interface SsoSession {
  user_name: string
  email: string
  role: string
  is_agency_owner: boolean
  company_id: string
  active_location: string | null
  scope: 'agency' | 'location'
}

export interface Rates {
  voice_rate_cents: number
  call_rate_cents: number
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

export interface ClientDetail {
  month: string
  location_id: string
  client_name: string
  mapped: true
  active: boolean
  billing_mode: string
  retell_agent_ids: string[]
  phone_numbers: string[]
  twilio_subaccount_sid: string
  rates: Rates | null
  rate_history: Rates[]
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
