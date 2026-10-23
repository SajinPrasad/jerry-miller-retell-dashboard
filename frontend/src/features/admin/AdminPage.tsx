import { useEffect, useState } from 'react'
import { getClientDetail, getClients, openSession } from '../../api/admin'
import { NoSessionError, requestSessionBlob } from '../../lib/ghlSession'
import type { ClientDetail as Detail, ClientsResponse, SsoSession } from '../../types/admin'
import './admin.css'
import ClientDetail from './ClientDetail'
import ClientsTable from './ClientsTable'
import { formatMonth } from './format'

type Status = 'loading' | 'ready' | 'no-session' | 'error'

export default function AdminPage() {
  const [status, setStatus] = useState<Status>('loading')
  const [session, setSession] = useState<SsoSession | null>(null)
  const [clients, setClients] = useState<ClientsResponse | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const blob = await requestSessionBlob()
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
        setStatus(err instanceof NoSessionError ? 'no-session' : 'error')
      }
    }

    void load()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!selected) return
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
  }, [selected])

  // Derived rather than cleared in an effect, so a half-loaded switch never shows the client
  // we just navigated away from under the name of the one we are opening.
  const openDetail = selected && detail?.location_id === selected ? detail : null

  if (status === 'loading') {
    return (
      <main className="admin">
        <p className="muted">Loading…</p>
      </main>
    )
  }

  if (status === 'no-session') {
    return (
      <main className="admin">
        <h1>Voice billing</h1>
        <div className="banner error" role="alert">
          Open this page from inside GoHighLevel. It needs the GoHighLevel session to know which
          account it is showing.
        </div>
      </main>
    )
  }

  if (status === 'error' || !clients) {
    return (
      <main className="admin">
        <h1>Voice billing</h1>
        <div className="banner error" role="alert">
          Could not load billing data. Refresh to try again.
        </div>
      </main>
    )
  }

  const isSubAccount = clients.scope === 'location'

  return (
    <main className="admin">
      <header className="admin-header">
        <div>
          <h1>Voice billing</h1>
          <p className="muted">
            {formatMonth(clients.month)}
            {session?.user_name && <> · {session.user_name}</>}
          </p>
        </div>
        {!isSubAccount && selected && (
          <button type="button" className="button ghost" onClick={() => setSelected(null)}>
            ← All clients
          </button>
        )}
      </header>

      {selected ? (
        openDetail ? (
          <ClientDetail detail={openDetail} />
        ) : (
          <p className="muted">Loading client…</p>
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
