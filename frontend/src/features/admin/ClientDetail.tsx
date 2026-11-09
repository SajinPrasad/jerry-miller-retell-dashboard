import { useState } from 'react'
import { requestCard, retryCharge, saveRates } from '../../api/admin'
import type {
  BillSummary,
  CardStatus,
  ClientDetail as Detail,
  MappingResult,
  RateFields,
  RecentCall,
  VoiceStatus,
} from '../../types/admin'
import { requestPreviewCard, retryPreviewCharge, savePreviewRates } from './demoData'
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
  /** The client's whole refreshed detail, as the rates endpoint answered it. */
  onRatesSaved: (detail: Detail) => void
}

/**
 * What the card row says, in the three words an admin acts on.
 *
 * The three "no card" statuses read the same deliberately: what differs is why, which is the
 * note beside them, and only two of them have a button that would help. `unknown` is never one
 * of them - saying "no card" because Stripe was briefly unreachable would send someone chasing
 * a client who has a perfectly good one.
 */
const CARD_LABELS: Record<CardStatus['status'], string> = {
  card_on_file: 'Card on file',
  no_card: 'No card on file',
  no_customer: 'No card on file',
  no_billing_email: 'No card on file',
  unknown: 'Could not check',
}

const CARD_TONES: Record<CardStatus['status'], string> = {
  card_on_file: 'ok',
  no_card: 'warn',
  no_customer: 'warn',
  no_billing_email: 'warn',
  unknown: '',
}

const CARD_NOTES: Partial<Record<CardStatus['status'], string>> = {
  no_customer: 'No Stripe customer matches this email yet.',
  no_billing_email: 'No email on the sub-account owner in GoHighLevel.',
  unknown: 'Stripe could not be reached just now.',
}

/** Stripe's own lowercase word for the card's brand, as it is written on the card: `visa`. */
function cardBrand(brand: string): string {
  return brand ? brand.charAt(0).toUpperCase() + brand.slice(1) : ''
}

/** What the headline tile is called once a bill exists, in the bill's own vocabulary. */
const BILL_LABELS: Record<BillSummary['status'], string> = {
  uncharged: 'Uncharged',
  charging: 'Charging',
  retrying: 'In Retry',
  paid: 'Paid',
  failed: 'Failed',
}

/**
 * The badge colour for a bill that needs attention. Amber while Stripe is still being retried,
 * red once the retries are spent and someone has to step in. Paid and the in-flight states
 * carry no badge: they are settled or on their way, not a problem to look at.
 */
const BILL_TONES: Partial<Record<BillSummary['status'], 'warn' | 'err'>> = {
  retrying: 'warn',
  failed: 'err',
}

export default function ClientDetail({
  detail,
  canEdit,
  preview,
  onMappingSaved,
  onRatesSaved,
}: Props) {
  const { usage, rates } = detail
  // What the retry button's own answer said, once one has been pressed. Held as an override
  // rather than as a copy of the whole bill, for the same reason the saved rates below are:
  // this component is not remounted when the page refreshes its detail, and a copy taken at
  // mount would quietly go stale. Null means nothing has been retried on this page.
  const [charged, setCharged] = useState<{ bill: BillSummary | null; voice: VoiceStatus } | null>(
    null,
  )
  const [retrying, setRetrying] = useState(false)
  const [retryError, setRetryError] = useState<string | null>(null)
  const bill = charged ? charged.bill : detail.bill
  const voice = charged ? charged.voice : detail.voice
  // Twilio rebilling is paused here whenever this is false: GoHighLevel already rebills those
  // minutes natively. Everything Twilio is hidden rather than removed, and the figures below
  // count Retell AI only - which is what the server's own estimate counts too.
  const twilioEnabled = detail.twilio_billing_enabled
  // A client nobody has set a rate for starts from the defaults rather than from $0.00, which
  // is both what saving would otherwise write and not a rate anyone chose.
  const storedVoice = rates?.voice_rate_cents ?? detail.default_rates.voice_rate_cents
  const storedCall = rates?.call_rate_cents ?? detail.default_rates.call_rate_cents

  // The typed rates. The page remounts per client and per period, so a rate typed here cannot
  // follow the viewer into another one.
  const [voiceInput, setVoiceInput] = useState(centsToInput(storedVoice))
  const [callInput, setCallInput] = useState(centsToInput(storedCall))
  // What the last save on this page wrote. A saved rate starts *today*, so on a past month
  // `detail.rates` deliberately does not move - without this the page would go on calling a
  // rate it has just saved unsaved.
  const [saved, setSaved] = useState<RateFields | null>(null)
  const [saving, setSaving] = useState(false)
  const [rateError, setRateError] = useState<string | null>(null)

  // Remounted per client and per period (see `ClientBrowser`'s `key`), so a tab chosen on one
  // client's page is never carried into another's - every page opens on Overview & billing.
  const [activeTab, setActiveTab] = useState<'overview' | 'mapping'>('overview')

  const voiceRate = centsFromInput(voiceInput)
  // The rates endpoint requires both rates whatever is on screen - they share one `RateHistory`
  // row. With the call-rate field hidden there is nothing typed to read, so the stored rate
  // stands in for it and goes back to the server unchanged rather than as a zero nobody chose.
  const callRate = twilioEnabled ? centsFromInput(callInput) : storedCall
  const baseline = saved ?? { voice_rate_cents: storedVoice, call_rate_cents: storedCall }
  const adjusted = voiceRate !== baseline.voice_rate_cents || callRate !== baseline.call_rate_cents

  // What the paused months cost Jerry is Retell alone: Twilio's own charges are GoHighLevel's
  // to rebill, so counting them here would understate the margin on money he never billed.
  const callCost = twilioEnabled ? usage.call_cost_cents : 0
  const cost = usage.ai_cost_cents + callCost
  // Until a rate is changed, the server's own figure stands. Recomputing it from minutes that
  // were rounded for display would let the headline drift a cent from what the backend says.
  const estimated = adjusted
    ? Math.round(
        Number(usage.ai_minutes) * voiceRate +
          (twilioEnabled ? Number(usage.call_minutes) * callRate : 0),
      )
    : usage.estimated_bill_cents
  // Once the month has been billed, the bill *is* the figure. It was worked out from the rate
  // in effect on each call's own day, so nothing typed here can move it.
  const headline = bill ? bill.total_cents : estimated

  /** Put the client on the system defaults. Saving is still a separate, deliberate click. */
  function resetToDefaults() {
    setVoiceInput(centsToInput(detail.default_rates.voice_rate_cents))
    // A rate nobody can see is not one anyone is resetting: while Twilio is paused the stored
    // call rate is left exactly where it is, and the save sends it back unchanged.
    if (twilioEnabled) setCallInput(centsToInput(detail.default_rates.call_rate_cents))
  }

  // Only a bill Stripe has an open invoice for can be collected again, and only an agency
  // admin may press it: a client re-running their own card is not something anyone asked for.
  const canRetry = canEdit && (bill?.status === 'failed' || bill?.status === 'retrying')

  async function handleRetryCharge() {
    setRetrying(true)
    setRetryError(null)
    try {
      setCharged(
        preview
          ? retryPreviewCharge(detail.location_id, detail.month)
          : await retryCharge(detail.location_id, detail.month),
      )
    } catch {
      // A declined card is not this: that comes back as a bill carrying Stripe's reason. This is
      // the bill being in no state to retry, or Stripe not answering at all.
      setRetryError(
        'Could not put this bill back through Stripe. Reload the page and check the bill, then try again.',
      )
    } finally {
      setRetrying(false)
    }
  }

  async function handleSaveRates() {
    setSaving(true)
    setRateError(null)
    const fields: RateFields = { voice_rate_cents: voiceRate, call_rate_cents: callRate }
    try {
      const refreshed = preview
        ? savePreviewRates(detail.location_id, fields, detail.month)
        : await saveRates(detail.location_id, fields, detail.month)
      setSaved(fields)
      onRatesSaved(refreshed)
    } catch {
      setRateError('Could not save the rates. Try again.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <div className="tabs" role="tablist" aria-label="Client detail sections">
        <button
          type="button"
          role="tab"
          id="tab-overview"
          aria-selected={activeTab === 'overview'}
          aria-controls="tabpanel-overview"
          className={`tab${activeTab === 'overview' ? ' active' : ''}`}
          onClick={() => setActiveTab('overview')}
        >
          Overview &amp; billing
        </button>
        <button
          type="button"
          role="tab"
          id="tab-mapping"
          aria-selected={activeTab === 'mapping'}
          aria-controls="tabpanel-mapping"
          className={`tab${activeTab === 'mapping' ? ' active' : ''}`}
          onClick={() => setActiveTab('mapping')}
        >
          Agent mapping
        </button>
      </div>

      {activeTab === 'overview' && (
        <div role="tabpanel" id="tabpanel-overview" aria-labelledby="tab-overview">
          <section className="card">
            <h2>{detail.client_name}</h2>
            <p className="muted">
              <code>{detail.location_id}</code>
              {!detail.active && <span className="pill">Billing paused</span>}
            </p>

            <div className="tiles">
              <Tile label="AI minutes" value={formatMinutes(usage.ai_minutes)} />
              {twilioEnabled && (
                <Tile
                  label="Call minutes"
                  value={
                    detail.twilio_subaccount_sid || usage.has_twilio_usage
                      ? formatMinutes(usage.call_minutes)
                      : DASH
                  }
                />
              )}
              <Tile
                label={bill ? BILL_LABELS[bill.status] : 'Estimated'}
                tone={bill ? BILL_TONES[bill.status] : undefined}
                value={formatCents(headline)}
                accent
              />
              <Tile label="Cost" value={formatCents(cost)} />
              <Tile
                label="Margin"
                value={formatCents(marginCents(headline, usage.ai_cost_cents, callCost))}
              />
            </div>
            <p className="muted small">
              {bill ? (
                <BillNote bill={bill} />
              ) : adjusted ? (
                <>
                  At the adjusted rates below, which are not saved yet. The billing run applies the
                  rate in effect on each call&apos;s own day.
                </>
              ) : (
                <>
                  Estimated at the rate in effect now. The billing run applies the rate in effect on
                  each call&apos;s own day.
                </>
              )}
            </p>
            {bill?.failure_reason && (
              // A retry is still on its way, so the reason is shown in the amber of a notice rather
              // than the red of a failure.
              <div
                className={bill.status === 'retrying' ? 'banner warn' : 'banner error'}
                role="alert"
              >
                {bill.failure_reason}
              </div>
            )}
            {canRetry && (
              <p className="actions">
                <button
                  className="button"
                  type="button"
                  onClick={handleRetryCharge}
                  disabled={retrying}
                >
                  {retrying && <span className="spinner" aria-hidden="true" />}
                  {retrying ? 'Charging…' : 'Retry payment'}
                </button>
                <span className="muted small">
                  {bill?.status === 'failed'
                    ? ' Automatic retries have ended. Charges the card on file now and resumes calls once paid.'
                    : ' Takes the card now, instead of waiting for the next automatic retry.'}
                </span>
              </p>
            )}
            {retryError && (
              <div className="banner error" role="alert">
                {retryError}
              </div>
            )}
            <VoiceNote bill={bill} voice={voice} />
          </section>

          <BillingContact detail={detail} canEdit={canEdit} preview={preview} />

          <section className="card">
            <h2>Recent AI calls</h2>
            {detail.recent_calls.length > 0 ? (
              <>
                <p className="muted small">
                  Times are UTC, the same day the call is billed under. Cost is what Retell charges
                  for the call.
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
                            <span className="pill ok">{billableLabel(call)}</span>
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

          <section className="card">
            <h2>Rates</h2>
            {canEdit ? (
              <>
                <div className="rate-editor">
                  <RateField
                    label="AI minute"
                    value={voiceInput}
                    onChange={setVoiceInput}
                    describedBy="rate-note"
                  />
                  {twilioEnabled && (
                    <RateField
                      label="Call minute"
                      value={callInput}
                      onChange={setCallInput}
                      describedBy="rate-note"
                    />
                  )}
                  <button
                    type="button"
                    className="button"
                    onClick={handleSaveRates}
                    disabled={saving}
                  >
                    {saving ? 'Saving…' : 'Save rates'}
                  </button>
                  <button type="button" className="button ghost" onClick={resetToDefaults}>
                    Reset to defaults
                  </button>
                </div>
                {rateError && (
                  <div className="banner error" role="alert">
                    {rateError}
                  </div>
                )}
              </>
            ) : (
              <dl className="facts">
                <dt>AI minute</dt>
                <dd>
                  {rates ? formatRate(rates.voice_rate_cents) : <span className="muted">Not set</span>}
                </dd>
                {twilioEnabled && (
                  <>
                    <dt>Call minute</dt>
                    <dd>
                      {rates ? (
                        formatRate(rates.call_rate_cents)
                      ) : (
                        <span className="muted">Not set</span>
                      )}
                    </dd>
                  </>
                )}
              </dl>
            )}
            <p className="muted small" id="rate-note">
              {rates ? (
                <>In effect since {formatDate(rates.start_date)}. </>
              ) : (
                <>No rate has been set for this client yet. </>
              )}
              {canEdit &&
                (adjusted ? (
                  <>Changing a rate only re-estimates this page until it is saved.</>
                ) : (
                  <>
                    A saved rate starts today. Months that have already ended keep the rate that was
                    in effect then.
                  </>
                ))}
            </p>

            {detail.rate_history.length > 1 && (
              <table className="rate-history-table">
                <thead>
                  <tr>
                    <th>From</th>
                    <th className="num">AI minute</th>
                    {twilioEnabled && <th className="num">Call minute</th>}
                  </tr>
                </thead>
                <tbody>
                  {detail.rate_history.map((rate) => (
                    <tr key={rate.start_date}>
                      <td>{formatDate(rate.start_date)}</td>
                      <td className="num">{formatRate(rate.voice_rate_cents)}</td>
                      {twilioEnabled && <td className="num">{formatRate(rate.call_rate_cents)}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {twilioEnabled && detail.twilio_days.length > 0 && (
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
        </div>
      )}

      {activeTab === 'mapping' && (
        <div role="tabpanel" id="tabpanel-mapping" aria-labelledby="tab-mapping">
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
                agentNames={detail.retell_agents}
                twilioEnabled={twilioEnabled}
                preview={preview}
                onSaved={onMappingSaved}
              />
            ) : (
              <dl className="facts">
                <dt>Retell agents</dt>
                <dd>
                  {detail.retell_agents.length > 0 ? (
                    detail.retell_agents
                      .map((agent) => agent.agent_name || agent.agent_id)
                      .join(', ')
                  ) : (
                    <span className="muted">None mapped</span>
                  )}
                </dd>
                {twilioEnabled && (
                  <>
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
                  </>
                )}
              </dl>
            )}
            <p className="muted small">
              Billing mode: {detail.billing_mode === 'auto_charge' ? 'Auto charge' : 'Send invoice'}
            </p>
          </section>
        </div>
      )}
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
 * Where a billable call stands, from the bill it is on rather than from the month's. "Billable"
 * means no bill has taken this call yet; once one has, saying it could still be billed would
 * read as a charge waiting to happen - or as a second one. The month's own bill cannot answer
 * this: a call made after that bill was charged is on no bill, and is billable still.
 */
function billableLabel(call: RecentCall): string {
  if (!call.bill_status) return 'Billable'
  return call.bill_status === 'paid' ? 'Paid' : 'Billed'
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

/**
 * What the headline figure means once the month has been billed.
 *
 * The amount is settled by then, so the note's job is to say where the money got to - and to
 * explain why editing a rate below leaves the figure where it is.
 */
/**
 * Whether this client's calls are still routing, said only when it is news.
 *
 * Paused is worth saying whenever it is true. The error case is the sharper one: a bill that has
 * failed beside a client who was *not* paused is a client taking calls they are not paying for,
 * and nothing else on the page would show it.
 */
function VoiceNote({ bill, voice }: { bill: BillSummary | null; voice: VoiceStatus }) {
  if (bill?.status === 'failed' && !voice.paused) {
    return (
      <div className="banner error" role="alert">
        {`Calls are still routing for this client. ${
          voice.error || 'GoHighLevel was not updated, so their Retell agent is still live.'
        }`}
      </div>
    )
  }
  if (!voice.paused) return null
  return (
    <div className="banner warn" role="status">
      {`Calls paused${voice.paused_at ? ` ${formatDateTime(voice.paused_at)} UTC` : ''}. `}
      {voice.routing_agent_id
        ? 'The Retell agent id is restored in GoHighLevel when the bill is paid.'
        : 'No Retell agent is mapped to this client, so paying will not restore calls.'}
    </div>
  )
}

function BillNote({ bill }: { bill: BillSummary }) {
  return (
    <>
      {bill.status === 'paid' && <>Billed and paid in full. </>}
      {bill.status === 'charging' && (
        <>Billed and on a Stripe invoice. Waiting for Stripe to confirm the payment. </>
      )}
      {bill.status === 'retrying' && (
        <>
          Billed. The last charge failed and will be tried again.{' '}
          <strong>{`Retry ${bill.retry_count} of ${bill.max_retries}`}</strong>
          {bill.next_retry_at && <> {`Next retry ${formatDateTime(bill.next_retry_at)} UTC.`}</>}{' '}
        </>
      )}
      {bill.status === 'failed' && <>Billed, but the last charge attempt failed. </>}
      {bill.status === 'uncharged' && <>Billed. The charge has not been attempted yet. </>}
      At the rate in effect on each call&apos;s own day, not at the rate below.
    </>
  )
}

function Tile({
  label,
  value,
  accent,
  tone,
}: {
  label: string
  value: string
  accent?: boolean
  tone?: 'warn' | 'err'
}) {
  return (
    <div className={`tile${accent ? ' accent' : ''}`}>
      <span className={tone ? `tile-label pill ${tone}` : 'tile-label'}>{label}</span>
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
function BillingContact({
  detail,
  canEdit,
  preview,
}: {
  detail: Detail
  canEdit: boolean
  preview: boolean
}) {
  const {
    owner_name,
    billing_email,
    stripe_customer_id,
    stripe_customer_name,
    billing_failure_reason,
    client_name,
  } = detail
  // The owner's name from GoHighLevel, or the sub-account's own name when nobody has set one -
  // either way, a name, never the raw id a reader could do nothing with.
  const subAccount = owner_name || client_name

  // The server's answer, kept here once a request has been made so the row shows what the
  // endpoint re-read from Stripe rather than the status the page was loaded with.
  const [card, setCard] = useState(detail.card)
  const [requesting, setRequesting] = useState(false)
  const [requestError, setRequestError] = useState<string | null>(null)
  // Only these two have anything a card request can fix. A client with no email has nowhere to
  // send a form, and `unknown` means we do not know there is a problem at all.
  const canRequest = canEdit && (card.status === 'no_card' || card.status === 'no_customer')

  async function handleRequestCard() {
    setRequesting(true)
    setRequestError(null)
    try {
      setCard(
        preview ? requestPreviewCard(detail.location_id) : await requestCard(detail.location_id),
      )
    } catch {
      setRequestError(
        'Could not ask GoHighLevel for a card. Check the contact and the card status field, then try again.',
      )
    } finally {
      setRequesting(false)
    }
  }

  return (
    <section className="card">
      <h2>Billing contact</h2>
      <dl className="facts">
        <dt>Card</dt>
        <dd>
          <span className={`pill ${CARD_TONES[card.status]}`}>{CARD_LABELS[card.status]}</span>
          {card.last4 && ` ${cardBrand(card.brand)} •••• ${card.last4}`}
          {CARD_NOTES[card.status] && (
            <span className="muted"> {CARD_NOTES[card.status]}</span>
          )}
        </dd>
        <dt>Stripe customer</dt>
        <dd>
          {stripe_customer_id ? (
            stripe_customer_name || <span className="muted">No name on file in Stripe</span>
          ) : (
            <span className="muted">Nothing matched this email yet</span>
          )}
        </dd>
        <dt>Billing email</dt>
        <dd>
          {billing_email || (
            <span className="muted">No email on the sub-account owner in GoHighLevel</span>
          )}
        </dd>
        <dt>Sub-account</dt>
        <dd>{subAccount}</dd>
      </dl>
      {canRequest && (
        <p className="actions">
          <button className="button" type="button" onClick={handleRequestCard} disabled={requesting}>
            {requesting ? 'Requesting…' : 'Request card'}
          </button>
          {card.requested_at && (
            <span className="muted small">
              {` Card requested ${formatDateTime(card.requested_at)} UTC.`}
            </span>
          )}
        </p>
      )}
      {requestError && (
        <div className="banner error" role="alert">
          {requestError}
        </div>
      )}
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
