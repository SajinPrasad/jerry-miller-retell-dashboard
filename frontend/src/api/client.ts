import { getSessionBlob } from '../lib/ghlSession'

export class ApiError extends Error {
  readonly status: number
  /** The error response's JSON body, when it had one. Most callers only need `status`; the
   * Housecall Pro setup reads it for the backend's own reason (a missing calendar team member,
   * a rejected API key). */
  readonly body: unknown

  constructor(status: number, path: string, body?: unknown) {
    super(`API ${status}: ${path}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
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
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => undefined)
    throw new ApiError(res.status, path, body)
  }
  return res.json() as Promise<T>
}
