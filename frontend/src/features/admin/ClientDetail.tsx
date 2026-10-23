import type { ClientDetail as Detail } from '../../types/admin'
import {
  DASH,
  formatCents,
  formatDate,
  formatMinutes,
  formatRate,
  marginCents,
} from './format'

export default function ClientDetail({ detail }: { detail: Detail }) {
  const { usage, rates } = detail
  const cost = usage.ai_cost_cents + usage.call_cost_cents

  return (
    <>
      <section className="card">
        <h2>{detail.client_name}</h2>
        <p className="muted">
          <code>{detail.location_id}</code>
          {!detail.active && <span className="pill">Billing paused</span>}
        </p>

        <div className="tiles">
          <Tile label="AI minutes" value={formatMinutes(usage.ai_minutes)} />
          <Tile
            label="Call minutes"
            value={
              detail.twilio_subaccount_sid || usage.has_twilio_usage
                ? formatMinutes(usage.call_minutes)
                : DASH
            }
          />
          <Tile label="Estimated" value={formatCents(usage.estimated_bill_cents)} accent />
          <Tile label="Cost to Jerry" value={formatCents(cost)} />
          <Tile
            label="Margin"
            value={formatCents(
              marginCents(usage.estimated_bill_cents, usage.ai_cost_cents, usage.call_cost_cents),
            )}
          />
        </div>
        <p className="muted small">
          Estimated at the rate in effect now. The billing run applies the rate in effect on each
          call&apos;s own day.
        </p>
      </section>

      <section className="card">
        <h2>Mapping</h2>
        <dl className="facts">
          <dt>Retell agents</dt>
          <dd>
            {detail.retell_agent_ids.length > 0 ? (
              detail.retell_agent_ids.map((id) => <code key={id}>{id}</code>)
            ) : (
              <span className="muted">None mapped</span>
            )}
          </dd>
          <dt>Phone numbers</dt>
          <dd>
            {detail.phone_numbers.length > 0 ? (
              detail.phone_numbers.map((number) => <code key={number}>{number}</code>)
            ) : (
              <span className="muted">None mapped</span>
            )}
          </dd>
          <dt>Twilio subaccount</dt>
          <dd>
            {detail.twilio_subaccount_sid ? (
              <code>{detail.twilio_subaccount_sid}</code>
            ) : (
              <span className="muted">Not billed for regular calls</span>
            )}
          </dd>
          <dt>Billing mode</dt>
          <dd>{detail.billing_mode === 'auto_charge' ? 'Auto charge' : 'Send invoice'}</dd>
        </dl>
      </section>

      <section className="card">
        <h2>Rates</h2>
        {rates ? (
          <p>
            <strong>{formatRate(rates.voice_rate_cents)}</strong> per AI minute,{' '}
            <strong>{formatRate(rates.call_rate_cents)}</strong> per call minute, since{' '}
            {formatDate(rates.start_date)}.
          </p>
        ) : (
          <p className="muted">No rate has been set for this client yet.</p>
        )}
        {detail.rate_history.length > 1 && (
          <table>
            <thead>
              <tr>
                <th>From</th>
                <th className="num">AI minute</th>
                <th className="num">Call minute</th>
              </tr>
            </thead>
            <tbody>
              {detail.rate_history.map((rate) => (
                <tr key={rate.start_date}>
                  <td>{formatDate(rate.start_date)}</td>
                  <td className="num">{formatRate(rate.voice_rate_cents)}</td>
                  <td className="num">{formatRate(rate.call_rate_cents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {detail.twilio_days.length > 0 && (
        <section className="card">
          <h2>Twilio calls by day</h2>
          <p className="muted small">
            Minutes as Twilio billed them, already rounded per call.
            {usage.twilio_as_of && <> Current through {formatDate(usage.twilio_as_of)}.</>}
            {usage.twilio_stale && <span className="pill warn">Stale</span>}
          </p>
          <table>
            <thead>
              <tr>
                <th>Day</th>
                <th className="num">Calls</th>
                <th className="num">Minutes</th>
                <th className="num">Twilio charges</th>
              </tr>
            </thead>
            <tbody>
              {detail.twilio_days.map((day) => (
                <tr key={day.usage_date}>
                  <td>{formatDate(day.usage_date)}</td>
                  <td className="num">{day.call_count}</td>
                  <td className="num">{formatMinutes(day.usage_minutes)}</td>
                  <td className="num muted">{formatCents(day.twilio_price_cents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="card">
        <h2>Recent AI calls</h2>
        {detail.recent_calls.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Started</th>
                <th>Direction</th>
                <th className="num">Minutes</th>
                <th>Billing</th>
              </tr>
            </thead>
            <tbody>
              {detail.recent_calls.map((call) => (
                <tr key={call.call_id}>
                  <td>{formatDate(call.started_at)}</td>
                  <td>{call.direction || DASH}</td>
                  <td className="num">{formatMinutes(call.billable_minutes)}</td>
                  <td>
                    {call.billing_status === 'billable' ? (
                      <span className="pill ok">Billable</span>
                    ) : (
                      <span className="pill warn" title={call.unbillable_reason}>
                        {call.unbillable_reason || 'Unbillable'}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No AI calls recorded this month.</p>
        )}
      </section>
    </>
  )
}

function Tile({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className={`tile${accent ? ' accent' : ''}`}>
      <span className="tile-label">{label}</span>
      <span className="tile-value">{value}</span>
    </div>
  )
}
