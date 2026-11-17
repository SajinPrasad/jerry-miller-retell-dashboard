import { ApiError } from '../../api/client'

/** What the backend said, in a sentence an admin can act on. */
export function describeHcpError(err: unknown, fallback: string): string {
  if (!(err instanceof ApiError)) return fallback
  const body = (err.body ?? {}) as { detail?: string; code?: string }
  switch (body.code) {
    case 'hcp_invalid_api_key':
      return 'Housecall Pro rejected that API key. Generate a new one under My Apps → API Key Management and paste it again.'
    case 'hcp_rate_limited':
      return 'Housecall Pro is rate limiting this account. Wait a moment and try again.'
    case 'hcp_unavailable':
      return 'Housecall Pro could not be reached. Try again shortly.'
    case 'ghl_unavailable':
      return 'GoHighLevel could not be reached. Try again shortly.'
    case 'backfill_running':
      return 'The backfill is already running.'
  }
  // The backend's own sentence for a refused step (already connected, nothing mapped yet, ...).
  return err.status === 400 && body.detail ? body.detail : fallback
}
