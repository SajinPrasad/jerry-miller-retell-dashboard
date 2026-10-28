import { useEffect, useState } from 'react'
import { getClientDetail, getClients, openSession, syncClients } from '../../api/admin'
import { ApiError } from '../../api/client'
import { requestSessionBlob } from '../../lib/ghlSession'
import type { ClientDetailResult, ClientsResponse, SsoSession } from '../../types/admin'
import './admin.css'
import ClientBrowser, { Shell } from './ClientBrowser'
import { currentMonth } from './format'
import { ClientsTableSkeleton } from './Skeleton'

/** How the page got (or failed to get) its GoHighLevel session. Resolved once, on mount. */
type Session =
  | { kind: 'loading' }
  | { kind: 'live'; session: SsoSession }
  | { kind: 'unframed' }
  | { kind: 'blocked' }
  | { kind: 'error' }

/**
 * The live admin page: the GHL Custom Page's `/admin` route. Always real data from the real
 * backend - the sample walkthrough lives on its own route, `/admin/preview`, and is never reached
 * from here.
 */
export default function AdminPage() {
  const [session, setSession] = useState<Session>({ kind: 'loading' })
  const [month, setMonth] = useState(currentMonth())
  const [clients, setClients] = useState<ClientsResponse | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<ClientDetailResult | null>(null)
  const [failed, setFailed] = useState(false)
  const [syncing, setSyncing] = useState(false)

  // 1. Resolve the session once. The period filter must not re-run the handshake.
  useEffect(() => {
    let cancelled = false

    async function resolve() {
      const blob = await requestSessionBlob()

      // Opened straight in a browser, with no GoHighLevel to ask, or framed by something that
      // never answered. There is no live data to show - see the sample at /admin/preview instead.
      if (!blob) {
        if (!cancelled) setSession({ kind: 'unframed' })
        return
      }

      try {
        const opened = await openSession(blob)
        if (cancelled) return
        setSession({ kind: 'live', session: opened })
        // Inside a sub-account there is one client and no list to choose from.
        setSelected(opened.active_location)
      } catch (err) {
        if (cancelled) return
        // A session that GHL gave us but the backend refused is a real misconfiguration —
        // the wrong Shared Secret, or another agency. Say so instead of quietly showing
        // samples, which would hide the problem.
        const refused = err instanceof ApiError && (err.status === 401 || err.status === 403)
        setSession({ kind: refused ? 'blocked' : 'error' })
      }
    }

    void resolve()
    return () => {
      cancelled = true
    }
  }, [])

  // 2. The clients list. It carries the period too — its unbillable count is for a month — so
  //    it follows whichever period the open client is being read in.
  useEffect(() => {
    if (session.kind !== 'live') return

    let cancelled = false
    getClients(month)
      .then((loaded) => {
        if (cancelled) return
        setClients(loaded)
        // Cleared here rather than before the request, so a retry that works drops the banner
        // without a render that briefly claims everything is fine.
        setFailed(false)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [session, month])

  // 3. The open client's detail, for the period chosen on its own page. An unmapped sub-account
  //    is a normal 200 (see ClientBrowser's UnmappedNotice), not a failure - only a real error
  //    lands in `failed`.
  useEffect(() => {
    if (!selected || session.kind !== 'live') return

    let cancelled = false
    getClientDetail(selected, month)
      .then((loaded) => {
        if (!cancelled) setDetail(loaded)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [selected, month, session])

  // Re-syncs sub-accounts from GoHighLevel, then swaps in the fresh list - no reload needed.
  async function handleSync() {
    setSyncing(true)
    try {
      const loaded = await syncClients(month)
      setClients(loaded)
      setFailed(false)
    } catch {
      setFailed(true)
    } finally {
      setSyncing(false)
    }
  }

  if (session.kind === 'loading') {
    return (
      <Shell>
        <section className="card">
          <h2>Clients</h2>
          <ClientsTableSkeleton />
        </section>
      </Shell>
    )
  }

  if (session.kind === 'unframed') {
    return (
      <Shell>
        <div className="banner warn" role="status">
          Open this page from inside GoHighLevel to see live billing data.{' '}
          <a href="/admin/preview">See a sample with demo data</a>.
        </div>
      </Shell>
    )
  }

  if (session.kind === 'blocked') {
    return (
      <Shell>
        <div className="banner error" role="alert">
          GoHighLevel sent a session this server could not accept. Check that the app&apos;s Shared
          Secret matches <code>GHL_APP_SSO_KEY</code>, and that the agency installed here is the
          one you are signed in to.
        </div>
      </Shell>
    )
  }

  if (session.kind === 'error' || failed) {
    return (
      <Shell>
        <div className="banner error" role="alert">
          Could not load billing data. Refresh to try again.
        </div>
      </Shell>
    )
  }

  // Showing the period that was asked for, not one still in flight: the month has to match, so
  // last month's figures never sit under this month's heading.
  const shown = clients?.month === month ? clients : null
  const openDetail =
    selected && detail?.location_id === selected && detail?.month === month ? detail : null

  return (
    <Shell userName={session.session.user_name}>
      <ClientBrowser
        month={month}
        onMonthChange={setMonth}
        selected={selected}
        onSelect={setSelected}
        onBack={() => setSelected(null)}
        shown={shown}
        openDetail={openDetail}
        onSync={handleSync}
        syncing={syncing}
      />
    </Shell>
  )
}
