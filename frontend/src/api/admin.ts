import type {
  ClientDetail,
  ClientDetailResult,
  ClientsResponse,
  MappingFields,
  MappingResult,
  RateFields,
  RetellAgent,
  SsoSession,
  TwilioSubaccount,
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

/** Set up or edit a client's mapping. Agency admins only - the backend refuses a location session. */
export function saveMapping(locationId: string, mapping: MappingFields): Promise<MappingResult> {
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
