import { useState } from 'react'
import type { ClientDetail as Detail, MappingResult, RecentCall } from '../../types/admin'
import {
  DASH,
  centsFromInput,
  centsToInput,
  formatCents,
  formatDate,
  formatDateTime,
  formatMinutes,
  formatPhone,
  formatRate,
  marginCents,
} from './format'
import MappingEditor from './MappingEditor'

interface Props {
  detail: Detail
  /** Agency scope only - opened inside a sub-account the page shows usage, not settings. */
  canEdit: boolean
  preview: boolean
  onMappingSaved: (result: MappingResult) => void
}

export default function ClientDetail({ detail, canEdit, preview, onMappingSaved }: Props) {
  const { usage, rates } = detail
  const storedVoice = rates?.voice_rate_cents ?? 0
  const storedCall = rates?.call_rate_cents ?? 0

  // What-if rates, held here and nowhere else: nothing is saved, and the next client or period
  // starts from the stored rates again (the page remounts this on either change).
  const [voiceInput, setVoiceInput] = useState(centsToInput(storedVoice))
  const [callInput, setCallInput] = useState(centsToInput(storedCall))
  const voiceRate = centsFromInput(voiceInput)
  const callRate = centsFromInput(callInput)
  const adjusted = voiceRate !== storedVoice || callRate !== storedCall

  const cost = usage.ai_cost_cents + usage.call_cost_cents
  // Until a rate is changed, the server's own figure stands. Recomputing it from minutes that
  // were rounded for display would let the headline drift a cent from what the backend says.
  const estimated = adjusted
    ? Math.round(Number(usage.ai_minutes) * voiceRate + Number(usage.call_minutes) * callRate)
    : usage.estimated_bill_cents

  function reset() {
    setVoiceInput(centsToInput(storedVoice))
    setCallInput(centsToInput(storedCall))
  }

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
          <Tile label="Estimated" value={formatCents(estimated)} accent />
          <Tile label="Cost" value={formatCents(cost)} />
          <Tile
            label="Margin"
            value={formatCents(marginCents(estimated, usage.ai_cost_cents, usage.call_cost_cents))}
          />
        </div>
        <p className="muted small">
          {adjusted ? (
            <>
              At the adjusted rates below, which are not saved. The billing run applies the rate in
              effect on each call&apos;s own day.
            </>
          ) : (
            <>
              Estimated at the rate in effect now. The billing run applies the rate in effect on
              each call&apos;s own day.
            </>
          )}
        </p>
      </section>

      <section className="card">
        <h2>Rates</h2>
        <div className="rate-editor">
          <RateField
            label="AI minute"
            value={voiceInput}
            onChange={setVoiceInput}
            describedBy="rate-note"
          />
          <RateField
            label="Call minute"
            value={callInput}
            onChange={setCallInput}
            describedBy="rate-note"
          />
          {adjusted && (
            <button type="button" className="button ghost" onClick={reset}>
              Reset
            </button>
          )}
        </div>
        <p className="muted small" id="rate-note">
          {rates ? (
            <>In effect since {formatDate(rates.start_date)}. </>
          ) : (
            <>No rate has been set for this client yet. </>
          )}
          Changing a rate here only re-estimates this page; nothing is saved.
        </p>

        {detail.rate_history.length > 1 && (
          <table className="rate-history-table">
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

      <section className="card">
        <h2>Mapping</h2>
        {canEdit ? (
          <MappingEditor
            locationId={detail.location_id}
            clientName={detail.client_name}
            initial={{
              twilio_subaccount_sid: detail.twilio_subaccount_sid,
              retell_agent_ids: detail.retell_agent_ids,
              phone_numbers: detail.phone_numbers,
            }}
            preview={preview}
            onSaved={onMappingSaved}
          />
        ) : (
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
          </dl>
        )}
        <p className="muted small">
          Billing mode: {detail.billing_mode === 'auto_charge' ? 'Auto charge' : 'Send invoice'}
        </p>
      </section>

      <BillingContact detail={detail} />

      {detail.twilio_days.length > 0 && (
        <section className="card">
          <h2>Twilio calls by day</h2>
          <p className="muted small">
            Minutes as Twilio billed them, already rounded per call.
            {usage.twilio_as_of && <> Current through {formatDate(usage.twilio_as_of)}.</>}
            {usage.twilio_stale && <span className="pill warn">Stale</span>}
          </p>
          <table className="twilio-table">
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
          <>
            <p className="muted small">
              Times are UTC, the same day the call is billed under. Cost is what Retell charges for
              the call.
            </p>
            <table className="calls-table">
              <thead>
                <tr>
                  <th>Date &amp; time</th>
                  <th>Phone number</th>
                  <th>Direction</th>
                  <th className="num">Duration</th>
                  <th className="num">Cost</th>
                  <th className="num">Status</th>
                </tr>
              </thead>
              <tbody>
                {detail.recent_calls.map((call) => (
                  <tr key={call.call_id}>
                    <td>{formatDateTime(call.started_at)}</td>
                    <td>{formatPhone(customerNumber(call))}</td>
                    <td className="direction">{call.direction || DASH}</td>
                    <td className="num">{formatMinutes(durationMinutes(call))}</td>
                    <td className="num muted">{formatCents(call.retell_cost_cents)}</td>
                    <td className="num">
                      {call.billing_status === 'billable' ? (
                        <span className="pill ok">Billable</span>
                      ) : (
                        <span className="pill warn" title={call.unbillable_reason}>
                          {unbillableLabel(call)}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <p className="muted">No AI calls recorded this month.</p>
        )}
      </section>
    </>
  )
}

/**
 * The other end of the call: the person, not the client's own line. Which field that is depends
 * on the direction, the same way the webhook decides which number to match a client on.
 */
function customerNumber(call: RecentCall): string {
  return call.direction === 'outbound' ? call.to_number : call.from_number
}

/** How long the call ran. Null when Retell has not reported a duration — which is also why it
 * would be unbillable, so the row reads as a dash rather than as a call of no length. */
function durationMinutes(call: RecentCall): string | null {
  return call.duration_ms === null ? null : (call.duration_ms / 60_000).toFixed(2)
}

/**
 * Why a call cannot be billed, short enough for a pill. The backend's full sentence stays on
 * hover; this follows the same order it decides in, so the two never disagree.
 */
function unbillableLabel(call: RecentCall): string {
  if (call.call_status && call.call_status !== 'ended') {
    return call.call_status.replace(/_/g, ' ').replace(/^./, (first) => first.toUpperCase())
  }
  if (call.duration_ms === null) return 'No duration'
  return 'No start time'
}

/**
 * A per-minute rate, in dollars. Text rather than a number field: a money amount is typed, not
 * stepped, and a half-typed "0." must survive long enough to finish.
 */
function RateField({
  label,
  value,
  onChange,
  describedBy,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  describedBy: string
}) {
  return (
    <label className="rate-field">
      <span className="tile-label">{label}</span>
      <span className="money">
        <span aria-hidden="true">$</span>
        <input
          type="text"
          inputMode="decimal"
          value={value}
          aria-label={`${label} rate`}
          aria-describedby={describedBy}
          onChange={(event) => onChange(event.target.value)}
        />
      </span>
    </label>
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

/**
 * Who GoHighLevel says pays, and whether Stripe has been matched to them.
 *
 * Nothing here is editable. The owner is read from the sub-account on every sync, and the
 * Stripe customer is matched by their email the first time a bill is charged - so the only
 * thing this page can usefully do is show the result, and say what went wrong when there isn't
 * one. That message is the whole of what an admin can act on: it names the address that matched
 * nothing, which is fixed in GoHighLevel or in Stripe, never here.
 */
function BillingContact({ detail }: { detail: Detail }) {
  const { owner_name, billing_email, stripe_customer_id, billing_failure_reason } = detail

  return (
    <section className="card">
      <h2>Billing contact</h2>
      <dl className="facts">
        <dt>Sub-account owner</dt>
        <dd>{owner_name || <span className="muted">Not set in GoHighLevel</span>}</dd>
        <dt>Billing email</dt>
        <dd>
          {billing_email || (
            <span className="muted">No email on the sub-account owner in GoHighLevel</span>
          )}
        </dd>
        <dt>Stripe customer</dt>
        <dd>
          {stripe_customer_id ? (
            <code>{stripe_customer_id}</code>
          ) : (
            <span className="muted">Matched on the first charge</span>
          )}
        </dd>
      </dl>
      {!stripe_customer_id && billing_failure_reason && (
        <div className="banner error" role="alert">
          {billing_failure_reason}
        </div>
      )}
      <p className="muted small">
        Taken from GoHighLevel and matched in Stripe automatically. Change it on the sub-account,
        not here.
      </p>
    </section>
  )
}
