import { useEffect, useState } from 'react'
import { getClientDetail, getClients, openSession } from '../../api/admin'
import { ApiError } from '../../api/client'
import { requestSessionBlob } from '../../lib/ghlSession'
import type { ClientDetail as Detail, ClientsResponse, SsoSession } from '../../types/admin'
import './admin.css'
import ClientDetail from './ClientDetail'
import ClientsTable from './ClientsTable'
import { DEMO_CLIENTS, DEMO_DETAILS, DEMO_SESSION } from './demoData'
import { formatMonth } from './format'
import { ClientDetailSkeleton, ClientsTableSkeleton } from './Skeleton'

type Status = 'loading' | 'ready' | 'blocked' | 'error'

export default function AdminPage() {
  const [status, setStatus] = useState<Status>('loading')
  const [session, setSession] = useState<SsoSession | null>(null)
  const [clients, setClients] = useState<ClientsResponse | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  // True when GoHighLevel never answered, so the page is showing bundled sample data. The
  // preview never calls the admin API, so it cannot surface a real client's figures.
  const [preview, setPreview] = useState(false)

  useEffect(() => {
    let cancelled = false

    async function load() {
      const blob = await requestSessionBlob()

      if (!blob) {
        // Opened straight in a browser, with no GoHighLevel to ask. Show the sample preview
        // rather than a dead end, clearly marked as such.
        if (cancelled) return
        setPreview(true)
        setSession(DEMO_SESSION)
        setClients(DEMO_CLIENTS)
        setStatus('ready')
        return
      }

      try {
        const openedSession = await openSession(blob)
        const list = await getClients()
        if (cancelled) return
        setSession(openedSession)
        setClients(list)
        // Inside a sub-account there is one client and no list to choose from.
        setSelected(openedSession.active_location)
        setStatus('ready')
      } catch (err) {
        if (cancelled) return
        // A session that GHL gave us but the backend refused is a real misconfiguration —
        // the wrong Shared Secret, or another agency. Say so instead of quietly showing
        // samples, which would hide the problem.
        setStatus(err instanceof ApiError && (err.status === 401 || err.status === 403)
          ? 'blocked'
          : 'error')
      }
    }

    void load()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    // The preview's detail is derived below, so there is nothing to fetch.
    if (!selected || preview) return
    let cancelled = false
    getClientDetail(selected)
      .then((loaded) => {
        if (!cancelled) setDetail(loaded)
      })
      .catch(() => {
        if (!cancelled) setStatus('error')
      })
    return () => {
      cancelled = true
    }
  }, [selected, preview])

  // Derived rather than cleared in an effect, so a half-loaded switch never shows the client
  // we just navigated away from under the name of the one we are opening.
  let openDetail: Detail | null = null
  if (selected) {
    openDetail = preview
      ? (DEMO_DETAILS[selected] ?? null)
      : detail?.location_id === selected
        ? detail
        : null
  }

  if (status === 'loading') {
    return (
      <main className="admin">
        <Header />
        <section className="card">
          <h2>Clients</h2>
          <ClientsTableSkeleton />
        </section>
      </main>
    )
  }

  if (status === 'blocked') {
    return (
      <main className="admin">
        <Header />
        <div className="banner error" role="alert">
          GoHighLevel sent a session this server could not accept. Check that the app&apos;s Shared
          Secret matches <code>GHL_APP_SSO_KEY</code>, and that the agency installed here is the
          one you are signed in to.
        </div>
      </main>
    )
  }

  if (status === 'error' || !clients) {
    return (
      <main className="admin">
        <Header />
        <div className="banner error" role="alert">
          Could not load billing data. Refresh to try again.
        </div>
      </main>
    )
  }

  const isSubAccount = clients.scope === 'location'

  return (
    <main className="admin">
      <Header month={clients.month} userName={session?.user_name}>
        {!isSubAccount && selected && (
          <button type="button" className="button ghost" onClick={() => setSelected(null)}>
            ← All clients
          </button>
        )}
      </Header>

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
            Usage this month per client. Amounts are estimates at each client&apos;s current rate.
          </p>
          <ClientsTable clients={clients.clients} onSelect={setSelected} />
        </section>
      )}
    </main>
  )
}

function Header({
  month,
  userName,
  children,
}: {
  month?: string
  userName?: string
  children?: React.ReactNode
}) {
  return (
    <header className="admin-header">
      <div>
        <h1>Voice billing</h1>
        <p className="muted">
          {month ? formatMonth(month) : 'Loading…'}
          {userName && <> · {userName}</>}
        </p>
      </div>
      {children}
    </header>
  )
}
