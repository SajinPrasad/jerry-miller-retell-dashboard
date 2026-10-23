import { getSessionBlob } from '../lib/ghlSession'

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const session = getSessionBlob()
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      // The GHL session is what authenticates an admin request. It goes in a header rather
      // than the URL so it stays out of logs and referrers.
      ...(session ? { 'X-GHL-Session': session } : {}),
      ...init?.headers,
    },
  })
  if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
  return res.json() as Promise<T>
}
