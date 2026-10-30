import type {
  ClientDetail as Detail,
  ClientDetailResult,
  ClientsResponse,
  MappingResult,
} from '../../types/admin'
import ClientDetail from './ClientDetail'
import ClientsTable from './ClientsTable'
import MappingEditor from './MappingEditor'
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
  /** True on the standalone preview, which never calls the real API - a mapping edit there only
   * updates the bundled sample data. */
  preview: boolean
  onMappingSaved: (result: MappingResult) => void
  /** The client's whole refreshed detail, as the rates endpoint answered it. */
  onRatesSaved: (detail: Detail) => void
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
  preview,
  onMappingSaved,
  onRatesSaved,
}: Props) {
  const isSubAccount = shown?.scope === 'location'
  // Agency admins set up and edit mapping; a sub-account's own page shows its usage, not settings.
  const canEdit = !isSubAccount

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
              canEdit ? (
                <SetUpMapping
                  locationId={openDetail.location_id}
                  clientName={openDetail.client_name}
                  preview={preview}
                  onSaved={onMappingSaved}
                />
              ) : (
                <UnmappedNotice clientName={openDetail.client_name} />
              )
            ) : (
              // Remounted per client and per period, so the what-if rates on it start from the
              // rates that client is actually on.
              <ClientDetail
                key={`${selected}:${month}`}
                detail={openDetail}
                canEdit={canEdit}
                preview={preview}
                onMappingSaved={onMappingSaved}
                onRatesSaved={onRatesSaved}
              />
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
            <ClientsTable clients={shown.clients} onSelect={onSelect} canEdit={canEdit} />
          ) : (
            <ClientsTableSkeleton />
          )}
        </section>
      )}
    </>
  )
}

/** What an agency admin sees on a client's page before anyone has set it up for billing: the
 * mapping editor instead of usage, with nothing yet to read. */
function SetUpMapping({
  locationId,
  clientName,
  preview,
  onSaved,
}: {
  locationId: string
  clientName: string
  preview: boolean
  onSaved: (result: MappingResult) => void
}) {
  return (
    <section className="card">
      <h2>{clientName || 'This client'}</h2>
      <p className="muted small">
        Not set up for billing yet. Add its Retell agents, phone numbers and Twilio subaccount to
        start tracking usage.
      </p>
      <MappingEditor
        locationId={locationId}
        clientName={clientName}
        initial={{ twilio_subaccount_sid: '', retell_agent_ids: [], phone_numbers: [] }}
        preview={preview}
        onSaved={onSaved}
      />
    </section>
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
