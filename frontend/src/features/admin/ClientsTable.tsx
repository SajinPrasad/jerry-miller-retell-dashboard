import type { ClientSummary } from '../../types/admin'
import { DASH } from './format'

interface Props {
  clients: ClientSummary[]
  onSelect: (locationId: string) => void
}

/**
 * Who is onboarded, and whether they are set up for billing. Nothing more: the figures live in
 * the client's own page, where there is room to show how they were arrived at.
 */
export default function ClientsTable({ clients, onSelect }: Props) {
  if (clients.length === 0) {
    return <p className="muted">No client sub-accounts yet. Connect GoHighLevel and sync them.</p>
  }

  return (
    <table className="clients">
      <thead>
        <tr>
          <th>Client</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {clients.map((client) => (
          <ClientRow key={client.location_id} client={client} onSelect={onSelect} />
        ))}
      </tbody>
    </table>
  )
}

function ClientRow({ client, onSelect }: { client: ClientSummary; onSelect: Props['onSelect'] }) {
  const { usage } = client

  if (!client.mapped || !usage) {
    return (
      <tr>
        <td>
          <span className="name">{client.client_name || DASH}</span>
        </td>
        <td>
          <span className="pill" title="Not set up for billing yet">
            Not mapped
          </span>
        </td>
      </tr>
    )
  }

  // One pill, with what it is mapped to on hover. The difference between Retell and Twilio
  // matters when something is missing, not on every row.
  const detail = mappingDetail(client)

  return (
    <tr className="clickable" onClick={() => onSelect(client.location_id)}>
      <td>
        <button type="button" className="link" onClick={() => onSelect(client.location_id)}>
          {client.client_name}
        </button>
      </td>
      <td>
        {client.active ? (
          <span className="pill ok" title={detail}>
            Mapped
          </span>
        ) : (
          <span className="pill" title={detail}>
            Paused
          </span>
        )}
        {usage.unbillable_calls > 0 && (
          <span className="pill warn" title="Calls that cannot be billed">
            {usage.unbillable_calls} unbillable
          </span>
        )}
      </td>
    </tr>
  )
}

/** What the pill means for this client, for its tooltip. */
function mappingDetail(client: ClientSummary): string {
  if (client.retell_connected && client.twilio_connected) return 'Retell AI & Twilio mapped'
  if (client.retell_connected) return 'Retell AI mapped only'
  if (client.twilio_connected) return 'Twilio mapped only'
  return 'No Retell agents or Twilio subaccount mapped'
}
