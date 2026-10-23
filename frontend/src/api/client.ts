import { getSessionBlob } from '../lib/ghlSession'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, path: string) {
    super(`API ${status}: ${path}`)
    this.name = 'ApiError'
    this.status = status
  }
}

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
  // The status matters to the caller: a 401 means the session was refused, which is a different
  // thing from the request failing.
  if (!res.ok) throw new ApiError(res.status, path)
  return res.json() as Promise<T>
}
