import type {
  BillSummary,
  CardStatus,
  ClientDetail,
  ClientDetailResult,
  ClientsResponse,
  MappingFields,
  MappingResult,
  RateFields,
  RetellAgent,
  SsoSession,
  TwilioSubaccount,
  VoiceStatus,
} from '../types/admin'
import { apiFetch } from './client'

/** Opens the session blob on the backend, which is the only place that can read it. */
export function openSession(key: string): Promise<SsoSession> {
  return apiFetch<SsoSession>('/ghl/sso/session/', {
    method: 'POST',
    body: JSON.stringify({ key }),
  })
}

export function getClients(month?: string): Promise<ClientsResponse> {
  return apiFetch<ClientsResponse>(`/rebilling/admin/clients/${monthQuery(month)}`)
}

/** Re-syncs sub-accounts from GoHighLevel, then returns the updated list. */
export function syncClients(month?: string): Promise<ClientsResponse> {
  return apiFetch<ClientsResponse>(`/rebilling/admin/clients/sync/${monthQuery(month)}`, {
    method: 'POST',
  })
}

export function getClientDetail(locationId: string, month?: string): Promise<ClientDetailResult> {
  return apiFetch<ClientDetailResult>(
    `/rebilling/admin/clients/${encodeURIComponent(locationId)}/${monthQuery(month)}`,
  )
}

/**
 * Set up or edit a client's mapping. Agency admins only - the backend refuses a location session.
 *
 * Partial because `set_mapping()` writes only the fields it is sent: while Twilio billing is
 * paused the editor leaves `twilio_subaccount_sid` and `phone_numbers` out entirely, which
 * preserves whatever is stored rather than clearing it.
 */
export function saveMapping(
  locationId: string,
  mapping: Partial<MappingFields>,
): Promise<MappingResult> {
  return apiFetch<MappingResult>(`/rebilling/admin/clients/${encodeURIComponent(locationId)}/mapping/`, {
    method: 'PUT',
    body: JSON.stringify(mapping),
  })
}

/**
 * Put a client on new per-minute rates, from today. Agency admins only.
 *
 * Answers with the client's whole refreshed detail for `month`, so the page shows the server's
 * own figures rather than guessing which of them the save moved - a rate saved today does not
 * reach back over a month that has already ended.
 */
export function saveRates(
  locationId: string,
  rates: RateFields,
  month?: string,
): Promise<ClientDetail> {
  return apiFetch<ClientDetail>(
    `/rebilling/admin/clients/${encodeURIComponent(locationId)}/rates/${monthQuery(month)}`,
    { method: 'PUT', body: JSON.stringify(rates) },
  )
}

/**
 * Ask a client for a card, by setting the card status on their GoHighLevel billing contact,
 * which is what starts the workflow emailing them a form. Agency admins only.
 *
 * Answers with the client's card status re-read from Stripe, so a client who attached one
 * between the page loading and the button being pressed comes back as having a card.
 */
export async function requestCard(locationId: string): Promise<CardStatus> {
  const body = await apiFetch<{ card: CardStatus }>(
    `/rebilling/admin/clients/${encodeURIComponent(locationId)}/card-request/`,
    { method: 'POST' },
  )
  return body.card
}

/**
 * Collect a failed or retrying bill now, instead of waiting for the next automatic retry.
 * Agency admins only.
 *
 * A declined card is a successful request: it answers with the bill carrying Stripe's reason and
 * the retry it has moved on to. Only a bill in no state to be retried, or a Stripe that could not
 * be reached, rejects - and then the bill is left exactly as it was.
 */
export function retryCharge(
  locationId: string,
  month?: string,
): Promise<{ month: string; bill: BillSummary | null; voice: VoiceStatus }> {
  return apiFetch(`/rebilling/admin/clients/${encodeURIComponent(locationId)}/retry-charge/`, {
    method: 'POST',
    body: JSON.stringify(month ? { month } : {}),
  })
}

/** Every agent on Jerry's Retell account, for the mapping editor's agent picker. */
export async function getRetellAgents(): Promise<RetellAgent[]> {
  const body = await apiFetch<{ agents: RetellAgent[] }>('/rebilling/admin/retell-agents/')
  return body.agents
}

/** Every subaccount under Jerry's Twilio parent account, for the subaccount picker. */
export async function getTwilioSubaccounts(): Promise<TwilioSubaccount[]> {
  const body = await apiFetch<{ subaccounts: TwilioSubaccount[] }>(
    '/rebilling/admin/twilio-subaccounts/',
  )
  return body.subaccounts
}

function monthQuery(month?: string): string {
  return month ? `?month=${encodeURIComponent(month)}` : ''
}
