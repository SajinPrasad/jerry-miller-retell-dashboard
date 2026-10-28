import type { ClientDetailResult, ClientsResponse, SsoSession } from '../types/admin'
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

function monthQuery(month?: string): string {
  return month ? `?month=${encodeURIComponent(month)}` : ''
}
