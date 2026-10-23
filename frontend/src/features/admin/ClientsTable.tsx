import type { ClientSummary } from '../../types/admin'
import { DASH, formatCents, formatMinutes, formatRate, marginCents } from './format'

interface Props {
  clients: ClientSummary[]
  onSelect: (locationId: string) => void
}

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
          <th className="num">AI minutes</th>
          <th className="num">Call minutes</th>
          <th className="num">Rates</th>
          <th className="num">Estimated</th>
          <th className="num">Cost</th>
          <th className="num">Margin</th>
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
  const { usage, rates } = client

  // An unmapped client has no usage to show. Dashes, never zeros: "0.00 minutes" would read as
  // a client who made no calls, when the truth is that nobody has set them up.
  if (!client.mapped || !usage) {
    return (
      <tr>
        <td>
          <span className="name">{client.client_name || DASH}</span>
          <code className="muted">{client.location_id}</code>
        </td>
        <td>
          <span className="pill">Not mapped</span>
        </td>
        <td className="num muted" colSpan={6}>
          Not set up for billing yet
        </td>
      </tr>
    )
  }

  const cost = usage.ai_cost_cents + usage.call_cost_cents

  return (
    <tr className="clickable" onClick={() => onSelect(client.location_id)}>
      <td>
        <button type="button" className="link" onClick={() => onSelect(client.location_id)}>
          {client.client_name}
        </button>
        <code className="muted">{client.location_id}</code>
      </td>
      <td>
        {client.active ? (
          <span className="pill ok">Mapped</span>
        ) : (
          <span className="pill">Paused</span>
        )}
        {client.twilio_connected && <span className="pill">Twilio</span>}
        {usage.twilio_stale && (
          <span className="pill warn" title="Twilio usage has not refreshed recently">
            Stale
          </span>
        )}
        {usage.unbillable_calls > 0 && (
          <span className="pill warn" title="Calls that cannot be billed">
            {usage.unbillable_calls} unbillable
          </span>
        )}
      </td>
      <td className="num">{formatMinutes(usage.ai_minutes)}</td>
      {/* A client with no Twilio subaccount reads as exactly that, not as zero usage. */}
      <td className="num">
        {client.twilio_connected || usage.has_twilio_usage ? (
          formatMinutes(usage.call_minutes)
        ) : (
          <span className="muted">{DASH}</span>
        )}
      </td>
      <td className="num small rates">
        {rates ? (
          <>
            {formatRate(rates.voice_rate_cents)} AI
            <br />
            {formatRate(rates.call_rate_cents)} call
          </>
        ) : (
          <span className="muted">No rate set</span>
        )}
      </td>
      <td className="num strong">{formatCents(usage.estimated_bill_cents)}</td>
      <td className="num muted">{formatCents(cost)}</td>
      <td className="num">
        {formatCents(marginCents(usage.estimated_bill_cents, usage.ai_cost_cents, usage.call_cost_cents))}
      </td>
    </tr>
  )
}
