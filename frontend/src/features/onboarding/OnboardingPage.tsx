import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getOnboardingStatus, syncLocations } from '../../api/ghl'
import type { OnboardingStatus } from '../../types/ghl'
import './onboarding.css'

// Reasons the backend OAuth callback sends back as `?error=` (see apps/ghl/views.py).
const ERRORS: Record<string, string> = {
  install_cancelled: 'The install was cancelled in GoHighLevel.',
  install_failed:
    'The install could not be completed. Make sure you install on the SurPro Labs agency, then try again.',
}

export default function OnboardingPage() {
  const [params] = useSearchParams()
  const [status, setStatus] = useState<OnboardingStatus | null>(null)
  const [error, setError] = useState('')
  const [syncing, setSyncing] = useState(false)

  useEffect(() => {
    getOnboardingStatus()
      .then(setStatus)
      .catch(() => setError('Could not load the connection status.'))
  }, [])

  const handleSync = async () => {
    setSyncing(true)
    setError('')
    try {
      setStatus(await syncLocations())
    } catch {
      setError('Could not refresh sub-accounts from GoHighLevel.')
    } finally {
      setSyncing(false)
    }
  }

  const callbackError = params.get('error')
  const agency = status?.agency

  return (
    <main className="onboarding">
      <h1>Connect GoHighLevel</h1>
      <p className="muted">
        Install the SurPro Labs app on the agency. It is then installed on every client
        sub-account, so the dashboards can read each client&apos;s data.
      </p>

      {callbackError && (
        <div className="banner error" role="alert">
          {ERRORS[callbackError] ?? ERRORS.install_failed}
        </div>
      )}
      {!callbackError && params.get('status') === 'connected' && (
        <div className="banner ok" role="status">
          Agency connected.
        </div>
      )}
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}

      {status && (
        <section className="card">
          <h2>Agency</h2>
          {agency ? (
            <p>
              <span className="pill ok">Connected</span> <code>{agency.company_id}</code>
            </p>
          ) : (
            <p>
              <span className="pill">Not connected</span> Sign in to GoHighLevel as an agency
              admin and choose the agency when asked.
            </p>
          )}
          <a className="button" href={status.install_url}>
            {agency ? 'Reconnect' : 'Connect to GoHighLevel'}
          </a>
        </section>
      )}

      {agency && status && (
        <section className="card">
          <h2>Client sub-accounts</h2>
          {status.locations.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Location ID</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {status.locations.map((location) => (
                  <tr key={location.location_id}>
                    <td>{location.name || '—'}</td>
                    <td>
                      <code>{location.location_id}</code>
                    </td>
                    <td>
                      <span className={`pill ${location.is_active ? 'ok' : ''}`}>
                        {location.is_active ? 'Active' : 'Uninstalled'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p>No sub-accounts found yet.</p>
          )}
          <button type="button" className="button" onClick={handleSync} disabled={syncing}>
            {syncing ? 'Refreshing…' : 'Refresh sub-accounts'}
          </button>
        </section>
      )}
    </main>
  )
}
