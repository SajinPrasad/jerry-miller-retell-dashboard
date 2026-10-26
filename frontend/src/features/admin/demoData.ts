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
  ClientDetail,
  ClientSummary,
  ClientsResponse,
  RecentCall,
  SsoSession,
  TwilioDay,
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
  voice_rate_cents: number
  call_rate_cents: number
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

export function demoClients(month: string): ClientsResponse {
  return {
    scope: 'agency',
    month,
    clients: [...SPECS.map((spec) => summary(spec, month)), UNMAPPED].sort((a, b) =>
      a.client_name.localeCompare(b.client_name),
    ),
  }
}

export function demoDetail(locationId: string, month: string): ClientDetail | null {
  const spec = SPECS.find((candidate) => candidate.location_id === locationId)
  if (!spec) return null

  const usage = usageFor(spec, month)
  return {
    month,
    location_id: spec.location_id,
    client_name: spec.client_name,
    active: true,
    billing_mode: 'auto_charge',
    retell_agent_ids: spec.agents,
    phone_numbers: spec.numbers,
    twilio_subaccount_sid: spec.twilio_sid,
    rates: { ...rate(spec), start_date: `${month}-15` },
    rate_history: [
      { ...rate(spec), start_date: `${month}-15` },
      ...(spec.previous_rate
        ? [{ ...spec.previous_rate, start_date: `${shiftBack(month)}-01` }]
        : []),
    ],
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

function rate(spec: Spec) {
  return { voice_rate_cents: spec.voice_rate_cents, call_rate_cents: spec.call_rate_cents }
}

function usageFor(spec: Spec, month: string): UsageSummary {
  const f = scale(spec.location_id, month)
  const aiMinutes = spec.ai_minutes * f
  const callMinutes = spec.call_minutes * f
  const aiCost = Math.round(spec.retell_cost_cents * f)
  const callCost = Math.round(spec.twilio_cost_cents * f)
  const estimated = Math.round(
    aiMinutes * spec.voice_rate_cents + callMinutes * spec.call_rate_cents,
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
