/**
 * Sample data for the standalone preview.
 *
 * Used only when the page is opened outside GoHighLevel, so the UI can be walked through on a
 * bare IP before the domain and the Custom Page exist. It is invented, it lives in the bundle,
 * and the preview path never calls the admin API — so previewing cannot expose a real client's
 * rates or call history to whoever reaches the server.
 *
 * The moment GoHighLevel answers the handshake, the real API takes over and none of this is
 * used. Delete this file once the page is reached through GHL for good.
 */

import type { ClientDetail, ClientSummary, ClientsResponse, SsoSession } from '../../types/admin'

const MONTH = thisMonth()

export const DEMO_SESSION: SsoSession = {
  user_name: 'Standalone preview',
  email: '',
  role: 'admin',
  is_agency_owner: true,
  company_id: 'preview',
  active_location: null,
  scope: 'agency',
}

const COOL_BREEZE: ClientSummary = {
  location_id: 'demo_cool_breeze',
  client_name: 'Cool Breeze HVAC',
  mapped: true,
  active: true,
  billing_mode: 'auto_charge',
  twilio_connected: true,
  rates: { voice_rate_cents: 25, call_rate_cents: 5, start_date: `${MONTH}-01` },
  usage: {
    ai_minutes: '198.40',
    ai_cost_cents: 1389,
    call_minutes: '124.00',
    call_cost_cents: 138,
    has_twilio_usage: true,
    estimated_bill_cents: 5580,
    unbillable_calls: 0,
    twilio_as_of: `${MONTH}-16T03:30:00Z`,
    twilio_stale: false,
  },
}

// Retell only: no Twilio subaccount, so call minutes must read as a dash, not as zero.
const BRANWYNN: ClientSummary = {
  location_id: 'demo_branwynn',
  client_name: 'Branwynn Destinations',
  mapped: true,
  active: true,
  billing_mode: 'auto_charge',
  twilio_connected: false,
  rates: { voice_rate_cents: 30, call_rate_cents: 5, start_date: `${MONTH}-01` },
  usage: {
    ai_minutes: '156.52',
    ai_cost_cents: 1094,
    call_minutes: '0.00',
    call_cost_cents: 0,
    has_twilio_usage: false,
    estimated_bill_cents: 4696,
    unbillable_calls: 2,
    twilio_as_of: null,
    twilio_stale: false,
  },
}

// A mid-month rate change, and a Twilio pull that has quietly stopped.
const POLAR_PEAK: ClientSummary = {
  location_id: 'demo_polar_peak',
  client_name: 'Polar Peak Heating & Air',
  mapped: true,
  active: true,
  billing_mode: 'auto_charge',
  twilio_connected: true,
  rates: { voice_rate_cents: 28, call_rate_cents: 6, start_date: `${MONTH}-15` },
  usage: {
    ai_minutes: '95.43',
    ai_cost_cents: 670,
    call_minutes: '39.00',
    call_cost_cents: 50,
    has_twilio_usage: true,
    estimated_bill_cents: 2906,
    unbillable_calls: 1,
    twilio_as_of: `${MONTH}-09T03:30:00Z`,
    twilio_stale: true,
  },
}

// Onboarded in GoHighLevel but never mapped for billing.
const SUMMIT: ClientSummary = {
  location_id: 'demo_summit_air',
  client_name: 'Summit Air Solutions',
  mapped: false,
  active: null,
  billing_mode: null,
  twilio_connected: false,
  rates: null,
  usage: null,
}

export const DEMO_CLIENTS: ClientsResponse = {
  scope: 'agency',
  month: MONTH,
  clients: [BRANWYNN, COOL_BREEZE, POLAR_PEAK, SUMMIT],
}

export const DEMO_DETAILS: Record<string, ClientDetail> = {
  [COOL_BREEZE.location_id]: {
    month: MONTH,
    location_id: COOL_BREEZE.location_id,
    client_name: COOL_BREEZE.client_name,
    active: true,
    billing_mode: 'auto_charge',
    retell_agent_ids: ['agent_cool_breeze'],
    phone_numbers: ['+12137771234'],
    twilio_subaccount_sid: 'ACdemocoolbreeze0000000000000001',
    rates: COOL_BREEZE.rates,
    rate_history: [COOL_BREEZE.rates!],
    usage: COOL_BREEZE.usage!,
    twilio_days: twilioDays(8, '5.50'),
    recent_calls: calls('agent_cool_breeze', '+12137771234', 6, 0),
  },
  [BRANWYNN.location_id]: {
    month: MONTH,
    location_id: BRANWYNN.location_id,
    client_name: BRANWYNN.client_name,
    active: true,
    billing_mode: 'auto_charge',
    retell_agent_ids: ['agent_branwynn'],
    phone_numbers: ['+19545550111', '+18005550199'],
    twilio_subaccount_sid: '',
    rates: BRANWYNN.rates,
    rate_history: [BRANWYNN.rates!],
    usage: BRANWYNN.usage!,
    twilio_days: [],
    recent_calls: calls('agent_branwynn', '+19545550111', 5, 2),
  },
  [POLAR_PEAK.location_id]: {
    month: MONTH,
    location_id: POLAR_PEAK.location_id,
    client_name: POLAR_PEAK.client_name,
    active: true,
    billing_mode: 'auto_charge',
    retell_agent_ids: ['agent_polar_peak'],
    phone_numbers: ['+16025550144'],
    twilio_subaccount_sid: 'ACdemopolarpeak00000000000000002',
    rates: POLAR_PEAK.rates,
    rate_history: [
      POLAR_PEAK.rates!,
      { voice_rate_cents: 20, call_rate_cents: 4, start_date: `${lastYear()}-10-01` },
    ],
    usage: POLAR_PEAK.usage!,
    twilio_days: twilioDays(6, '3.00'),
    recent_calls: calls('agent_polar_peak', '+16025550144', 5, 1),
  },
}

function thisMonth(): string {
  const now = new Date()
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}`
}

function lastYear(): number {
  return new Date().getUTCFullYear() - 1
}

function twilioDays(count: number, minutes: string) {
  return Array.from({ length: count }, (_, index) => ({
    usage_date: `${MONTH}-${String(index + 1).padStart(2, '0')}`,
    category: 'calls',
    call_count: 3 + (index % 5),
    usage_minutes: (Number(minutes) + (index % 4)).toFixed(2),
    twilio_price_cents: Math.max(Math.round((Number(minutes) + (index % 4)) * 1.3), 1),
    as_of: `${MONTH}-${String(count + 1).padStart(2, '0')}T03:30:00Z`,
  }))
}

function calls(agentId: string, number: string, billable: number, unbillable: number) {
  const ok = Array.from({ length: billable }, (_, index) => ({
    call_id: `demo_${agentId}_${index}`,
    agent_id: agentId,
    from_number: index % 2 ? '+15550000001' : number,
    to_number: index % 2 ? number : '+15550000001',
    direction: index % 2 ? 'inbound' : 'outbound',
    started_at: `${MONTH}-${String(index + 2).padStart(2, '0')}T14:0${index}:00Z`,
    duration_ms: 180_000 + index * 23_000,
    billable_minutes: ((180_000 + index * 23_000) / 60_000).toFixed(2),
    retell_cost_cents: 21 + index * 2,
    call_status: 'ended',
    billing_status: 'billable' as const,
    unbillable_reason: '',
  }))

  const bad = Array.from({ length: unbillable }, (_, index) => ({
    call_id: `demo_${agentId}_bad_${index}`,
    agent_id: agentId,
    from_number: '+15550000009',
    to_number: number,
    direction: 'inbound',
    started_at: `${MONTH}-${String(index + 3).padStart(2, '0')}T11:00:00Z`,
    duration_ms: null,
    billable_minutes: '0.00',
    retell_cost_cents: null,
    call_status: index % 2 === 0 ? 'error' : 'not_connected',
    billing_status: 'unbillable' as const,
    unbillable_reason:
      index % 2 === 0
        ? 'Call status is error, not ended.'
        : 'Call status is not_connected, not ended.',
  }))

  return [...bad, ...ok]
}
