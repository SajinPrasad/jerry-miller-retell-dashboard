import type { ClientDetailResult, ClientsResponse } from '../../types/admin'
import ClientDetail from './ClientDetail'
import ClientsTable from './ClientsTable'
import MonthPicker from './MonthPicker'
import { ClientDetailSkeleton, ClientsTableSkeleton } from './Skeleton'

/** The page chrome every state of the admin page shares: the title and who is viewing it. */
export function Shell({
  userName,
  children,
}: {
  userName?: string
  children: React.ReactNode
}) {
  return (
    <main className="admin">
      <header className="admin-header">
        <div>
          <h1>Voice billing</h1>
          <p className="muted">{userName ?? 'Loading…'}</p>
        </div>
      </header>
      {children}
    </main>
  )
}

interface Props {
  banner?: React.ReactNode
  month: string
  onMonthChange: (month: string) => void
  selected: string | null
  onSelect: (locationId: string) => void
  onBack: () => void
  shown: ClientsResponse | null
  openDetail: ClientDetailResult | null
  /** Omitted on the standalone preview, which never calls the real API. */
  onSync?: () => void
  syncing?: boolean
}

/**
 * The list-or-detail body shared by the live page and the standalone preview, so the two render
 * identically and only differ in where their data comes from.
 */
export default function ClientBrowser({
  banner,
  month,
  onMonthChange,
  selected,
  onSelect,
  onBack,
  shown,
  openDetail,
  onSync,
  syncing,
}: Props) {
  const isSubAccount = shown?.scope === 'location'

  return (
    <>
      {banner}

      {selected ? (
        <>
          {/* The period belongs to the client being read, so the picker lives here, on the
              same line as the way back. */}
          <div className="detail-bar">
            {!isSubAccount && (
              <button type="button" className="button ghost" onClick={onBack}>
                ← All clients
              </button>
            )}
            <MonthPicker month={month} onChange={onMonthChange} busy={!openDetail} />
          </div>
          {openDetail ? (
            openDetail.mapped === false ? (
              <UnmappedNotice clientName={openDetail.client_name} />
            ) : (
              // Remounted per client and per period, so the what-if rates on it start from the
              // rates that client is actually on.
              <ClientDetail key={`${selected}:${month}`} detail={openDetail} />
            )
          ) : (
            <ClientDetailSkeleton />
          )}
        </>
      ) : (
        <section className="card">
          <div className="card-header">
            <h2>Clients</h2>
            {onSync && (
              <button type="button" className="button ghost" onClick={onSync} disabled={syncing}>
                {syncing ? 'Refreshing…' : 'Refresh clients'}
              </button>
            )}
          </div>
          <p className="muted small">
            Every sub-account onboarded in GoHighLevel. Open one for its minutes, rates and bill
            estimate.
          </p>
          {shown ? (
            <ClientsTable clients={shown.clients} onSelect={onSelect} />
          ) : (
            <ClientsTableSkeleton />
          )}
        </section>
      )}
    </>
  )
}

/** What a client sees on its own page before an agency admin has set it up for billing. */
function UnmappedNotice({ clientName }: { clientName: string }) {
  return (
    <section className="card">
      <h2>{clientName || 'This client'}</h2>
      <p className="muted">Voice billing isn&apos;t set up for this client yet.</p>
      <p className="muted small">
        This is being configured. Check back soon, or reach out to SurPro Labs AI if you have
        questions.
      </p>
    </section>
  )
}
