/**
 * Sample data for the standalone preview.
 *
 * Used only when the page is opened outside GoHighLevel, so the UI can be walked through on a
 * bare IP before the domain and the Custom Page exist. It is invented, it lives in the bundle,
 * and the preview path never calls the admin API — so previewing cannot expose a real client's
 * rates or call history to whoever reaches the server.
 *
 * Figures vary by billing month, deterministically, so the period filter visibly does something
 * here as well as against the real API. The current month keeps its headline numbers so the
 * preview reads the same way every time it is opened.
 *
 * The moment GoHighLevel answers the handshake, the real API takes over and none of this is
 * used. Delete this file once the page is reached through GHL for good.
 */

import type {
  BillSummary,
  ClientDetail,
  ClientDetailResult,
  ClientSummary,
  ClientsResponse,
  MappingFields,
  RateFields,
  RecentCall,
  RetellAgent,
  SsoSession,
  TwilioDay,
  TwilioSubaccount,
  UsageSummary,
} from '../../types/admin'
import { currentMonth } from './format'

export const DEMO_SESSION: SsoSession = {
  user_name: 'Standalone preview',
  email: '',
  role: 'admin',
  is_agency_owner: true,
  company_id: 'preview',
  active_location: null,
  scope: 'agency',
}

interface Spec {
  location_id: string
  client_name: string
  agents: string[]
  numbers: string[]
  twilio_sid: string
  /** Read-only on the real page too: taken from the GHL sub-account owner, matched in Stripe. */
  owner_name: string
  billing_email: string
  stripe_customer_id: string
  /** Set only on the sample client Stripe could not match, so the preview shows that state. */
  billing_failure_reason?: string
  voice_rate_cents: number
  call_rate_cents: number
  /** How a finished month's bill ended up, so the preview shows a settled month as settled
   * rather than as an estimate. Absent means this client has never been billed. */
  bill_status?: BillSummary['status']
  bill_failure_reason?: string
  /** Headline figures for the current month; other months scale off these. */
  ai_minutes: number
  retell_cost_cents: number
  call_minutes: number
  twilio_cost_cents: number
  twilio_day_count: number
  unbillable: number
  stale: boolean
  /** A second, older rate, so the detail view has a history worth showing. */
  previous_rate?: { voice_rate_cents: number; call_rate_cents: number }
}

const SPECS: Spec[] = [
  {
    location_id: 'demo_cool_breeze',
    client_name: 'Cool Breeze HVAC',
    agents: ['agent_cool_breeze'],
    numbers: ['+12137771234'],
    twilio_sid: 'ACdemocoolbreeze0000000000000001',
    owner_name: 'Dana Reyes',
    billing_email: 'dana@coolbreeze.test',
    stripe_customer_id: 'cus_demoCoolBreeze',
    bill_status: 'paid',
    voice_rate_cents: 25,
    call_rate_cents: 5,
    ai_minutes: 198.4,
    retell_cost_cents: 1389,
    call_minutes: 124,
    twilio_cost_cents: 138,
    twilio_day_count: 8,
    unbillable: 0,
    stale: false,
  },
  {
    // Retell only: no Twilio subaccount, so call minutes must read as a dash, not as zero.
    location_id: 'demo_branwynn',
    client_name: 'Branwynn Destinations',
    agents: ['agent_branwynn'],
    numbers: ['+19545550111', '+18005550199'],
    twilio_sid: '',
    owner_name: 'Imogen Vale',
    billing_email: 'imogen@branwynn.test',
    stripe_customer_id: '',
    billing_failure_reason:
      'No Stripe customer found for billing email: imogen@branwynn.test',
    voice_rate_cents: 30,
    call_rate_cents: 5,
    ai_minutes: 156.52,
    retell_cost_cents: 1094,
    call_minutes: 0,
    twilio_cost_cents: 0,
    twilio_day_count: 0,
    unbillable: 2,
    stale: false,
  },
  {
    // A mid-month rate change, and a Twilio pull that has quietly stopped.
    location_id: 'demo_polar_peak',
    client_name: 'Polar Peak Heating & Air',
    agents: ['agent_polar_peak'],
    numbers: ['+16025550144'],
    twilio_sid: 'ACdemopolarpeak00000000000000002',
    owner_name: 'Marcus Hale',
    billing_email: 'marcus@polarpeak.test',
    stripe_customer_id: 'cus_demoPolarPeak',
    bill_status: 'failed',
    bill_failure_reason: 'card_declined',
    voice_rate_cents: 28,
    call_rate_cents: 6,
    ai_minutes: 95.43,
    retell_cost_cents: 670,
    call_minutes: 39,
    twilio_cost_cents: 50,
    twilio_day_count: 6,
    unbillable: 1,
    stale: true,
    previous_rate: { voice_rate_cents: 20, call_rate_cents: 4 },
  },
]

// Onboarded in GoHighLevel but never mapped for billing. The same in every period.
const UNMAPPED: ClientSummary = {
  location_id: 'demo_summit_air',
  client_name: 'Summit Air Solutions',
  mapped: false,
  active: null,
  billing_mode: null,
  retell_connected: false,
  twilio_connected: false,
  rates: null,
  usage: null,
}

// Edits the preview's MappingEditor has made, held outside React state so every demoData
// function reads the same in-memory values without a store of its own. Never sent anywhere -
// the standalone preview never calls the real API.
const previewMappings = new Map<string, MappingFields>()

export function savePreviewMapping(locationId: string, mapping: MappingFields): void {
  previewMappings.set(locationId, mapping)
}

export function resetPreviewMappings(): void {
  previewMappings.clear()
}

// The same, for rates. Held here rather than in React state for the same reason, and never
// sent anywhere - the standalone preview never calls the rates endpoint either.
const previewRates = new Map<string, RateFields>()

/** Stands in for `saveRates` in the preview, answering with the detail it would have returned. */
export function savePreviewRates(
  locationId: string,
  rates: RateFields,
  month: string,
): ClientDetail {
  previewRates.set(locationId, rates)
  return demoDetail(locationId, month) as ClientDetail
}

export function resetPreviewRates(): void {
  previewRates.clear()
}

/** `spec` with any preview edit to its mapping applied. Everything else - rates, call volume -
 * is untouched: the editor only ever writes the three mapping fields. */
function withPreviewMapping(spec: Spec): Spec {
  const edit = previewMappings.get(spec.location_id)
  if (!edit) return spec
  return {
    ...spec,
    agents: edit.retell_agent_ids,
    numbers: edit.phone_numbers,
    twilio_sid: edit.twilio_subaccount_sid,
  }
}

/**
 * What the mapping editor's pickers offer in the preview, in place of a live call to Retell.
 *
 * The agents the sample clients already use, plus a spare apiece, so the unmapped sample client
 * can be set up here the same way a real one is.
 */
export function demoRetellAgents(): RetellAgent[] {
  return [
    ...SPECS.flatMap((spec) =>
      spec.agents.map((agent_id) => ({ agent_id, agent_name: `${spec.client_name} — main line` })),
    ),
    { agent_id: 'agent_summit_air', agent_name: 'Summit Air Solutions — main line' },
    { agent_id: 'agent_after_hours', agent_name: 'Shared after-hours line' },
  ]
}

/** The same, for the Twilio picker. The parent account is not among them, as on the real one. */
export function demoTwilioSubaccounts(): TwilioSubaccount[] {
  return [
    ...SPECS.filter((spec) => spec.twilio_sid).map((spec) => ({
      sid: spec.twilio_sid,
      friendly_name: spec.client_name,
    })),
    { sid: 'ACdemosummitair00000000000000003', friendly_name: 'Summit Air Solutions' },
  ]
}

/** The backend's own defaults, which is what its detail response hands the page. */
const DEFAULT_RATES: RateFields = { voice_rate_cents: 25, call_rate_cents: 5 }

const EMPTY_USAGE: UsageSummary = {
  ai_minutes: '0.00',
  ai_cost_cents: 0,
  call_minutes: '0.00',
  call_cost_cents: 0,
  has_twilio_usage: false,
  // No rate has been set for a mapping this new, same as it would be on the real backend.
  estimated_bill_cents: null,
  unbillable_calls: 0,
  twilio_as_of: null,
  twilio_stale: false,
}

/** The sample unmapped client, with a preview edit applied once an admin has set it up. */
function unmappedOrSetUp(): ClientSummary {
  const edit = previewMappings.get(UNMAPPED.location_id)
  if (!edit) return UNMAPPED
  return {
    ...UNMAPPED,
    mapped: true,
    active: true,
    billing_mode: 'auto_charge',
    retell_connected: edit.retell_agent_ids.length > 0,
    twilio_connected: Boolean(edit.twilio_subaccount_sid),
    rates: null,
    usage: EMPTY_USAGE,
  }
}

export function demoClients(month: string): ClientsResponse {
  return {
    scope: 'agency',
    month,
    clients: [
      ...SPECS.map((spec) => summary(withPreviewMapping(spec), month)),
      unmappedOrSetUp(),
    ].sort((a, b) => a.client_name.localeCompare(b.client_name)),
  }
}

export function demoDetail(locationId: string, month: string): ClientDetailResult | null {
  if (locationId === UNMAPPED.location_id) {
    const edit = previewMappings.get(locationId)
    if (!edit) return { month, location_id: locationId, client_name: UNMAPPED.client_name, mapped: false }
    return {
      month,
      location_id: locationId,
      client_name: UNMAPPED.client_name,
      mapped: true,
      active: true,
      billing_mode: 'auto_charge',
      retell_agent_ids: edit.retell_agent_ids,
      phone_numbers: edit.phone_numbers,
      twilio_subaccount_sid: edit.twilio_subaccount_sid,
      owner_name: 'Priya Nandakumar',
      billing_email: 'priya@summitair.test',
      stripe_customer_id: '',
      billing_failure_reason: '',
      rates: null,
      rate_history: [],
      default_rates: DEFAULT_RATES,
      bill: null,
      usage: EMPTY_USAGE,
      twilio_days: [],
      recent_calls: [],
    }
  }

  const found = SPECS.find((candidate) => candidate.location_id === locationId)
  if (!found) return null
  const spec = withPreviewMapping(found)

  const usage = usageFor(spec, month)
  return {
    month,
    location_id: spec.location_id,
    client_name: spec.client_name,
    mapped: true,
    active: true,
    billing_mode: 'auto_charge',
    retell_agent_ids: spec.agents,
    phone_numbers: spec.numbers,
    twilio_subaccount_sid: spec.twilio_sid,
    owner_name: spec.owner_name,
    billing_email: spec.billing_email,
    stripe_customer_id: spec.stripe_customer_id,
    billing_failure_reason: spec.billing_failure_reason ?? '',
    rates: { ...rate(spec), start_date: `${month}-15` },
    rate_history: [
      { ...rate(spec), start_date: `${month}-15` },
      ...(spec.previous_rate
        ? [{ ...spec.previous_rate, start_date: `${shiftBack(month)}-01` }]
        : []),
    ],
    default_rates: DEFAULT_RATES,
    bill: billFor(spec, month, usage),
    usage,
    twilio_days: twilioDays(spec, month),
    recent_calls: recentCalls(spec, month),
  }
}

function summary(spec: Spec, month: string): ClientSummary {
  return {
    location_id: spec.location_id,
    client_name: spec.client_name,
    mapped: true,
    active: true,
    billing_mode: 'auto_charge',
    retell_connected: spec.agents.length > 0,
    twilio_connected: Boolean(spec.twilio_sid),
    rates: { ...rate(spec), start_date: `${month}-01` },
    usage: usageFor(spec, month),
  }
}

function rate(spec: Spec): RateFields {
  return (
    previewRates.get(spec.location_id) ?? {
      voice_rate_cents: spec.voice_rate_cents,
      call_rate_cents: spec.call_rate_cents,
    }
  )
}

/**
 * The sample bill for a finished month, or none at all.
 *
 * Only a month that has ended has one: the current month is still being used, which is exactly
 * the state the page's own estimate is for. The amount is what the estimate said, because on
 * the real backend that is where a bill comes from.
 */
function billFor(spec: Spec, month: string, usage: UsageSummary): BillSummary | null {
  if (!spec.bill_status || month >= currentMonth()) return null
  return {
    status: spec.bill_status,
    total_cents: usage.estimated_bill_cents ?? 0,
    failure_reason: spec.bill_failure_reason ?? '',
  }
}

function usageFor(spec: Spec, month: string): UsageSummary {
  const f = scale(spec.location_id, month)
  const rates = rate(spec)
  const aiMinutes = spec.ai_minutes * f
  const callMinutes = spec.call_minutes * f
  const aiCost = Math.round(spec.retell_cost_cents * f)
  const callCost = Math.round(spec.twilio_cost_cents * f)
  const estimated = Math.round(
    aiMinutes * rates.voice_rate_cents + callMinutes * rates.call_rate_cents,
  )
  const days = twilioDayCount(spec, month)

  return {
    ai_minutes: aiMinutes.toFixed(2),
    ai_cost_cents: aiCost,
    call_minutes: callMinutes.toFixed(2),
    call_cost_cents: callCost,
    has_twilio_usage: days > 0,
    estimated_bill_cents: estimated,
    unbillable_calls: f === 0 ? 0 : spec.unbillable,
    twilio_as_of: days > 0 ? `${month}-${pad(days + 1)}T03:30:00Z` : null,
    // Only the current month can be behind: an old month is settled, not stale.
    twilio_stale: spec.stale && days > 0 && month === currentMonth(),
  }
}

/**
 * A stable multiplier per client per month, so stepping through periods shows different but
 * repeatable figures. The current month is left at 1 so the preview's headline numbers are
 * the same every time someone opens it. A finished month is always above 1 — partly because a
 * month that has run its course really is busier than one halfway through, and partly so no
 * other period can land on exactly the figures shown for this one.
 */
function scale(locationId: string, month: string): number {
  if (month === currentMonth()) return 1
  if (month > currentMonth()) return 0
  let hash = 7
  for (const character of `${locationId}:${month}`) {
    hash = (hash * 31 + character.charCodeAt(0)) % 100_000
  }
  return 1.05 + (hash % 40) / 100
}

function twilioDayCount(spec: Spec, month: string): number {
  if (!spec.twilio_day_count || scale(spec.location_id, month) === 0) return 0
  return Math.max(Math.round(spec.twilio_day_count * scale(spec.location_id, month)), 1)
}

function twilioDays(spec: Spec, month: string): TwilioDay[] {
  const count = twilioDayCount(spec, month)
  if (!count) return []
  const perDay = (spec.call_minutes * scale(spec.location_id, month)) / count

  return Array.from({ length: count }, (_, index) => ({
    usage_date: `${month}-${pad(index + 1)}`,
    category: 'calls',
    call_count: 3 + (index % 5),
    usage_minutes: perDay.toFixed(2),
    twilio_price_cents: Math.max(Math.round(perDay * 1.3), 1),
    as_of: `${month}-${pad(count + 1)}T03:30:00Z`,
  }))
}

function recentCalls(spec: Spec, month: string): RecentCall[] {
  const f = scale(spec.location_id, month)
  if (f === 0) return []
  const billable = Math.max(Math.round(6 * f), 1)

  const unbillable: RecentCall[] = Array.from({ length: spec.unbillable }, (_, index) => ({
    call_id: `demo_${spec.location_id}_${month}_bad_${index}`,
    agent_id: spec.agents[0],
    from_number: caller(index + 40),
    to_number: spec.numbers[0],
    direction: 'inbound',
    started_at: `${month}-${pad(index + 3)}T${pad(8 + index * 3)}:${pad((index * 23) % 60)}:00Z`,
    duration_ms: null,
    billable_minutes: '0.00',
    retell_cost_cents: null,
    call_status: index % 2 === 0 ? 'error' : 'not_connected',
    billing_status: 'unbillable',
    unbillable_reason:
      index % 2 === 0
        ? 'Call status is error, not ended.'
        : 'Call status is not_connected, not ended.',
  }))

  const ok: RecentCall[] = Array.from({ length: billable }, (_, index) => {
    const durationMs = Math.round((180_000 + index * 23_000) * f)
    return {
      call_id: `demo_${spec.location_id}_${month}_${index}`,
      agent_id: spec.agents[0],
      // The client's own line is one end; a different caller is the other, so the preview's
      // phone column reads like a real one instead of the same number every row.
      from_number: index % 2 ? caller(index) : spec.numbers[0],
      to_number: index % 2 ? spec.numbers[0] : caller(index),
      direction: index % 2 ? 'inbound' : 'outbound',
      started_at: `${month}-${pad(index + 2)}T${pad(9 + (index % 9))}:${pad((index * 17) % 60)}:00Z`,
      duration_ms: durationMs,
      billable_minutes: (durationMs / 60_000).toFixed(2),
      retell_cost_cents: Math.max(Math.round((durationMs / 60_000) * 7), 1),
      call_status: 'ended',
      billing_status: 'billable',
      unbillable_reason: '',
    }
  })

  return [...unbillable, ...ok]
}

function shiftBack(month: string): string {
  const [year, number] = month.split('-').map(Number)
  const moved = new Date(Date.UTC(year, number - 7, 1))
  return `${moved.getUTCFullYear()}-${pad(moved.getUTCMonth() + 1)}`
}

/** A different member of the public each row, in a range reserved for fiction. */
function caller(index: number): string {
  return `+1213555${String(1000 + ((index * 137) % 9000))}`
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}
