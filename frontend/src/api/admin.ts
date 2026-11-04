import type {
  CardStatus,
  ClientDetail,
  ClientDetailResult,
  ClientsResponse,
  MappingFields,
  MappingResult,
  RateFields,
  RetellKeyTest,
  RetellWorkspaceSaved,
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
 * Ask Retell whether this key works, and which agents it can see. Writes nothing.
 *
 * The agent names are the point: a valid key belonging to *another* client's workspace
 * authenticates, lists, and would then bill this client for someone else's calls. Nothing
 * automatic can catch that - someone reading the names is the check.
 */
export function testRetellKey(locationId: string, apiKey: string): Promise<RetellKeyTest> {
  return apiFetch<RetellKeyTest>(
    `/rebilling/admin/clients/${encodeURIComponent(locationId)}/retell-key/`,
    { method: 'POST', body: JSON.stringify({ api_key: apiKey }) },
  )
}

/**
 * Save the key against the client. The server re-tests it first, whatever the page believes,
 * and answers with the last four characters - never the key.
 */
export function saveRetellKey(
  locationId: string,
  apiKey: string,
  workspaceName: string,
): Promise<RetellWorkspaceSaved> {
  return apiFetch<RetellWorkspaceSaved>(
    `/rebilling/admin/clients/${encodeURIComponent(locationId)}/retell-key/`,
    { method: 'PUT', body: JSON.stringify({ api_key: apiKey, workspace_name: workspaceName }) },
  )
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
