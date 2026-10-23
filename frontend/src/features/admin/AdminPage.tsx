import { useEffect, useState } from 'react'
import { getClientDetail, getClients, openSession } from '../../api/admin'
import { ApiError } from '../../api/client'
import { requestSessionBlob } from '../../lib/ghlSession'
import type { ClientDetail as Detail, ClientsResponse, SsoSession } from '../../types/admin'
import './admin.css'
import ClientDetail from './ClientDetail'
import ClientsTable from './ClientsTable'
import { DEMO_SESSION, demoClients, demoDetail } from './demoData'
import { currentMonth, formatMonth } from './format'
import MonthPicker from './MonthPicker'
import { ClientDetailSkeleton, ClientsTableSkeleton } from './Skeleton'

/** How the page got (or failed to get) its GoHighLevel session. Resolved once, on mount. */
type Session =
  | { kind: 'loading' }
  | { kind: 'live'; session: SsoSession }
  | { kind: 'preview' }
  | { kind: 'blocked' }
  | { kind: 'error' }

export default function AdminPage() {
  const [session, setSession] = useState<Session>({ kind: 'loading' })
  const [month, setMonth] = useState(currentMonth())
  const [clients, setClients] = useState<ClientsResponse | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [failed, setFailed] = useState(false)

  const preview = session.kind === 'preview'

  // 1. Resolve the session once. The period filter must not re-run the handshake.
  useEffect(() => {
    let cancelled = false

    async function resolve() {
      const blob = await requestSessionBlob()

      // Opened straight in a browser, with no GoHighLevel to ask. Show the sample preview
      // rather than a dead end, clearly marked as such.
      if (!blob) {
        if (!cancelled) setSession({ kind: 'preview' })
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

  // 2. The clients list, for whichever period is selected. The preview's list is derived
  //    below instead, since it is a pure function of the month.
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

  // 3. The open client's detail, for the same period.
  useEffect(() => {
    if (!selected || session.kind === 'preview' || session.kind !== 'live') return

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
      <Shell month={month}>
        <div className="banner error" role="alert">
          Could not load billing data. Refresh to try again.
        </div>
      </Shell>
    )
  }

  // Showing the period that was asked for, not one still in flight: the month has to match, so
  // last month's figures never sit under this month's heading.
  const shown = preview ? demoClients(month) : clients?.month === month ? clients : null
  const openDetail = resolveDetail()
  const isSubAccount = shown?.scope === 'location'

  function resolveDetail(): Detail | null {
    if (!selected) return null
    if (preview) return demoDetail(selected, month)
    return detail?.location_id === selected && detail?.month === month ? detail : null
  }

  return (
    <Shell
      month={month}
      onMonthChange={setMonth}
      busy={!shown}
      userName={session.kind === 'live' ? session.session.user_name : DEMO_SESSION.user_name}
      action={
        !isSubAccount && selected ? (
          <button type="button" className="button ghost" onClick={() => setSelected(null)}>
            ← All clients
          </button>
        ) : null
      }
    >
      {preview && (
        <div className="banner warn" role="status">
          <strong>Standalone preview.</strong> These are sample figures, not live billing data.
          Open this page from inside GoHighLevel to see real clients.
        </div>
      )}

      {selected ? (
        openDetail ? (
          <ClientDetail detail={openDetail} />
        ) : (
          <ClientDetailSkeleton />
        )
      ) : (
        <section className="card">
          <h2>Clients</h2>
          <p className="muted small">
            Usage in {formatMonth(month)} per client. Amounts are estimates at each client&apos;s
            current rate.
          </p>
          {shown ? (
            <ClientsTable clients={shown.clients} onSelect={setSelected} />
          ) : (
            <ClientsTableSkeleton />
          )}
        </section>
      )}
    </Shell>
  )
}

function Shell({
  month,
  onMonthChange,
  busy,
  userName,
  action,
  children,
}: {
  month?: string
  onMonthChange?: (month: string) => void
  busy?: boolean
  userName?: string
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <main className="admin">
      <header className="admin-header">
        <div>
          <h1>Voice billing</h1>
          <p className="muted">{userName ?? 'Loading…'}</p>
        </div>
        <div className="admin-controls">
          {month && onMonthChange && (
            <MonthPicker month={month} onChange={onMonthChange} busy={busy} />
          )}
          {action}
        </div>
      </header>
      {children}
    </main>
  )
}
