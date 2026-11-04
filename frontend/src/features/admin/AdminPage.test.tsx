import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  BillSummary,
  CardStatus,
  ClientDetail,
  ClientSummary,
  ClientsResponse,
  RateFields,
  RecentCall,
  SsoSession,
} from '../../types/admin'
import AdminPage from './AdminPage'
import { currentMonth, formatMonth, shiftMonth } from './format'

const BLOB = 'U2FsdGVkX1-pretend-session'
const THIS_MONTH = currentMonth()
const LAST_MONTH = shiftMonth(THIS_MONTH, -1)
/** "Oct", for whichever month the tests are running in. */
const MONTH_ABBREVIATION = formatMonth(THIS_MONTH).slice(0, 3)

const agencySession: SsoSession = {
  user_name: 'Jerry Miller',
  email: 'jerry@example.test',
  role: 'admin',
  is_agency_owner: true,
  company_id: 'comp_demo',
  active_location: null,
  scope: 'agency',
}

const mapped: ClientSummary = {
  location_id: 'loc1',
  client_name: 'Cool Breeze HVAC',
  mapped: true,
  active: true,
  billing_mode: 'auto_charge',
  retell_connected: true,
  twilio_connected: true,
  rates: { voice_rate_cents: 25, call_rate_cents: 5, start_date: `${THIS_MONTH}-01` },
  usage: {
    ai_minutes: '200.00',
    ai_cost_cents: 1800,
    call_minutes: '100.00',
    call_cost_cents: 420,
    has_twilio_usage: true,
    estimated_bill_cents: 5500,
    unbillable_calls: 0,
    twilio_as_of: `${THIS_MONTH}-20T03:30:00Z`,
    twilio_stale: false,
  },
}

const unmapped: ClientSummary = {
  location_id: 'loc-new',
  client_name: 'Polar Peak Heating',
  mapped: false,
  active: null,
  billing_mode: null,
  retell_connected: false,
  twilio_connected: false,
  rates: null,
  usage: null,
}

const detail: ClientDetail = {
  month: THIS_MONTH,
  location_id: 'loc1',
  client_name: 'Cool Breeze HVAC',
  mapped: true,
  // The fixture keeps Twilio on, so every test below that does not say otherwise proves the
  // Twilio half still works. The pause is covered in its own describe block.
  twilio_billing_enabled: true,
  active: true,
  billing_mode: 'auto_charge',
  retell_agent_ids: ['agent_1'],
  retell_agents: [{ agent_id: 'agent_1', agent_name: 'Cool Breeze front desk' }],
  // Set up, checked, and heard from - so every test that does not say otherwise proves the
  // healthy state. The states worth worrying about have their own tests.
  retell_workspace: {
    workspace_name: 'Cool Breeze HVAC',
    key_last4: '3a9f',
    status: 'active',
    last_checked_at: `${THIS_MONTH}-20T03:00:00Z`,
    last_webhook_at: `${THIS_MONTH}-20T11:15:00Z`,
    webhook_url: 'https://surpro.test/api/rebilling/webhooks/retell/loc1/',
  },
  phone_numbers: ['+15551234567'],
  twilio_subaccount_sid: 'ACchild',
  owner_name: 'Dana Reyes',
  billing_email: 'dana@coolbreeze.test',
  stripe_customer_id: 'cus_coolbreeze',
  stripe_customer_name: 'Cool Breeze HVAC LLC',
  billing_failure_reason: '',
  // The fixture client has a card, so every test that does not say otherwise proves the page
  // shows one and offers nothing to press. The states with a button have their own block.
  card: { status: 'card_on_file', brand: 'visa', last4: '4242', requested_at: null },
  rates: { voice_rate_cents: 25, call_rate_cents: 5, start_date: `${THIS_MONTH}-01` },
  rate_history: [{ voice_rate_cents: 25, call_rate_cents: 5, start_date: `${THIS_MONTH}-01` }],
  default_rates: { voice_rate_cents: 25, call_rate_cents: 5 },
  // Nothing billed for a month still in progress, so the page shows its own estimate.
  bill: null,
  usage: mapped.usage!,
  twilio_days: [],
  recent_calls: [
    {
      call_id: 'call_ok',
      agent_id: 'agent_1',
      from_number: '+12135557788',
      to_number: '+15551234567',
      direction: 'inbound',
      started_at: `${THIS_MONTH}-03T14:45:00Z`,
      duration_ms: 222_000,
      billable_minutes: '3.70',
      retell_cost_cents: 26,
      call_status: 'ended',
      billing_status: 'billable',
      unbillable_reason: '',
      bill_status: null,
    },
    {
      call_id: 'call_dropped',
      agent_id: 'agent_1',
      from_number: '+12135550022',
      to_number: '+15551234567',
      direction: 'inbound',
      started_at: `${THIS_MONTH}-04T09:05:00Z`,
      duration_ms: null,
      billable_minutes: '0.00',
      retell_cost_cents: null,
      call_status: 'not_connected',
      billing_status: 'unbillable',
      unbillable_reason: 'Call status is not_connected, not ended.',
      bill_status: null,
    },
  ],
}

const NEW_SID = 'AC' + '7'.repeat(32)

/**
 * What `POST|PUT /admin/clients/<id>/retell-key/` answers: the agents in *that client's own
 * workspace*, which is all their key can see. There is no account-wide catalogue any more.
 */
const workspaceAgents = {
  agents: [
    { agent_id: 'agent_1', agent_name: 'Cool Breeze front desk' },
    { agent_id: 'agent_2', agent_name: 'Cool Breeze after hours' },
  ],
}

/** What `GET /admin/twilio-subaccounts/` answers. The parent account is never among them. */
const twilioSubaccounts = {
  subaccounts: [
    { sid: 'ACchild', friendly_name: 'Cool Breeze HVAC' },
    { sid: NEW_SID, friendly_name: 'Polar Peak Heating' },
  ],
}

/**
 * Stand in for the GHL parent frame.
 *
 * `window.parent` has to differ from `window`, because that is how the page tells "framed by
 * GoHighLevel" from "opened directly in a browser" — in jsdom they are the same object.
 */
function framedBy(postMessage: (message: unknown) => void) {
  vi.spyOn(window, 'parent', 'get').mockReturnValue({ postMessage } as unknown as Window)
}

/** A parent that answers the handshake, the way GoHighLevel does. */
function parentAnswers(payload: unknown = BLOB) {
  framedBy((message) => {
    if ((message as { message?: string })?.message !== 'REQUEST_USER_DATA') return
    window.dispatchEvent(
      new MessageEvent('message', { data: { message: 'REQUEST_USER_DATA_RESPONSE', payload } }),
    )
  })
}

/** A parent that is there but never replies. */
function parentIgnores() {
  framedBy(() => {})
}

/**
 * The admin API, echoing back the month it was asked for — which is what the real endpoints do,
 * and what the page relies on to tell this period's figures from one still in flight.
 *
 * `perMonth` overrides the client returned for a given month. `synced` overrides what the
 * "Refresh clients" sync endpoint returns, in place of the regular list's own clients.
 */
function mockApi(
  options: {
    session?: SsoSession
    perMonth?: Record<string, ClientSummary>
    synced?: ClientSummary[]
    /** The bill the detail endpoint reports for the month being read. */
    bill?: BillSummary | null
    /** The month's calls, when a test needs them on bills of their own. */
    calls?: RecentCall[]
    /** Overrides onto the fixture detail, for a field no other option already covers. */
    detail?: Partial<ClientDetail>
    /** False once Twilio rebilling is paused, which both endpoints report the same way. */
    twilio?: boolean
    /** What the card request endpoint answers with, or a status code for one that refuses. */
    cardRequest?: CardStatus | number
  } = {},
) {
  const { session = agencySession, perMonth = {}, bill = null, twilio = true } = options

  return vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = String(input)
    const month = new URL(url, 'http://test').searchParams.get('month') ?? THIS_MONTH

    const json = (body: unknown) =>
      Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )

    if (url.startsWith('/api/ghl/sso/session/')) return json(session)
    if (url.startsWith('/api/rebilling/admin/twilio-subaccounts/')) return json(twilioSubaccounts)

    // Before the detail branch below, which this URL also starts with.
    if (url.startsWith('/api/rebilling/admin/clients/loc1/card-request/')) {
      const answer = options.cardRequest ?? REQUESTED
      if (typeof answer === 'number') {
        return Promise.resolve(new Response('{}', { status: answer }))
      }
      return json({ card: answer })
    }

    if (url.startsWith('/api/rebilling/admin/clients/loc1/')) {
      return json({
        ...detail,
        month,
        bill,
        twilio_billing_enabled: twilio,
        recent_calls: options.calls ?? detail.recent_calls,
        usage: (perMonth[month] ?? mapped).usage,
        ...options.detail,
      })
    }

    if (url.startsWith('/api/rebilling/admin/clients/sync/')) {
      const body: ClientsResponse = {
        scope: session.scope,
        month,
        twilio_billing_enabled: twilio,
        clients: [...(options.synced ?? [perMonth[month] ?? mapped, unmapped])],
      }
      return json(body)
    }

    if (url.startsWith('/api/rebilling/admin/clients/')) {
      const body: ClientsResponse = {
        scope: session.scope,
        month,
        twilio_billing_enabled: twilio,
        clients: session.scope === 'location' ? [mapped] : [perMonth[month] ?? mapped, unmapped],
      }
      return json(body)
    }

    return Promise.resolve(new Response('', { status: 404 }))
  })
}

function monthSelect() {
  return screen.getByLabelText('Billing month') as HTMLSelectElement
}

/** The tile with this label, which is where the detail page's headline figures live. */
function tile(label: string) {
  // By the label element, since a column header can carry the same word.
  const heading = screen.getByText(label, { selector: '.tile-label' })
  return within(heading.closest('.tile') as HTMLElement)
}

/** Open a client from the list and wait for its page. */
const NO_CARD: CardStatus = { status: 'no_card', brand: '', last4: '', requested_at: null }

/** What the endpoint answers with once a card has been asked for and Stripe still has none. */
const REQUESTED: CardStatus = { ...NO_CARD, requested_at: `${THIS_MONTH}-10T14:20:00Z` }

async function openClient(user: ReturnType<typeof userEvent.setup>, name = 'Cool Breeze HVAC') {
  await user.click(await screen.findByRole('button', { name }))
  await screen.findByText('Retell workspace')
}

describe('AdminPage', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    // A developer's .env.local would otherwise supply a blob and skip the handshake these
    // tests exist to cover.
    vi.stubEnv('VITE_DEV_SSO_BLOB', '')
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
  })

  it('asks the parent frame for the session and lists the clients', async () => {
    parentAnswers()
    mockApi()

    render(<AdminPage />)

    expect(await screen.findByText('Cool Breeze HVAC')).toBeInTheDocument()
    expect(screen.getByText(/Jerry Miller/)).toBeInTheDocument()
  })

  it('sends the session blob on every admin request', async () => {
    parentAnswers()
    const fetchMock = mockApi()

    render(<AdminPage />)
    await screen.findByText('Cool Breeze HVAC')

    const clientsCall = fetchMock.mock.calls.find(([url]) =>
      String(url).startsWith('/api/rebilling/admin/clients/'),
    )
    const headers = (clientsCall?.[1]?.headers ?? {}) as Record<string, string>
    expect(headers['X-GHL-Session']).toBe(BLOB)
  })

  // --- the list: who is onboarded, and nothing else ------------------------------------

  it('lists who is onboarded and their status, and no figures', async () => {
    parentAnswers()
    mockApi()

    render(<AdminPage />)
    await screen.findByText('Cool Breeze HVAC')

    expect(screen.getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Client',
      'Status',
    ])
    // The money and the minutes belong to the client's own page.
    expect(screen.queryByText('200.00')).not.toBeInTheDocument()
    expect(screen.queryByText('$55.00')).not.toBeInTheDocument()
    // No location id under the name: it is a code, not a business.
    expect(screen.queryByText('loc1')).not.toBeInTheDocument()
    // The period is chosen on the client's page, not here.
    expect(screen.queryByLabelText('Billing month')).not.toBeInTheDocument()
  })

  it('says what a client is mapped to on hover rather than in its own pill', async () => {
    parentAnswers()
    mockApi({
      perMonth: { [THIS_MONTH]: { ...mapped, twilio_connected: false } },
    })

    render(<AdminPage />)
    await screen.findByText('Cool Breeze HVAC')

    expect(screen.getByText('Mapped')).toHaveAttribute('title', 'Retell AI mapped only')
    expect(screen.queryByText('Twilio')).not.toBeInTheDocument()
  })

  it('keeps the unbillable warning in the list', async () => {
    parentAnswers()
    mockApi({
      perMonth: { [THIS_MONTH]: { ...mapped, usage: { ...mapped.usage!, unbillable_calls: 1 } } },
    })

    render(<AdminPage />)

    expect(await screen.findByText('1 unbillable')).toBeInTheDocument()
  })

  it('shows an unmapped client as not set up rather than as zero usage', async () => {
    parentAnswers()
    mockApi()

    render(<AdminPage />)
    await screen.findByText('Polar Peak Heating')

    expect(screen.getByText('Not mapped')).toHaveAttribute('title', 'Not set up for billing yet')
    expect(screen.queryByText('0.00')).not.toBeInTheDocument()
  })

  it('shows a skeleton while the clients are loading', async () => {
    parentAnswers()
    mockApi()

    render(<AdminPage />)

    expect(screen.getByLabelText('Loading clients')).toBeInTheDocument()
    await screen.findByText('Cool Breeze HVAC')
    expect(screen.queryByLabelText('Loading clients')).not.toBeInTheDocument()
  })

  // --- the "Refresh clients" button -------------------------------------------------------

  it('discovers a newly onboarded client and updates the table in place, with no reload', async () => {
    parentAnswers()
    const justOnboarded: ClientSummary = {
      ...unmapped,
      location_id: 'loc-brand-new',
      client_name: 'Brand New HVAC',
    }
    mockApi({ synced: [mapped, unmapped, justOnboarded] })
    const user = userEvent.setup()

    render(<AdminPage />)
    await screen.findByText('Cool Breeze HVAC')
    expect(screen.queryByText('Brand New HVAC')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Refresh clients' }))

    expect(await screen.findByText('Brand New HVAC')).toBeInTheDocument()
    // Still the same client list in the same page - not a reload.
    expect(screen.getByText('Cool Breeze HVAC')).toBeInTheDocument()
  })

  it('disables the button and says so while the sync is running', async () => {
    parentAnswers()
    const fetchMock = mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await screen.findByText('Cool Breeze HVAC')

    fetchMock.mockImplementation(() => new Promise(() => {}))
    await user.click(screen.getByRole('button', { name: 'Refresh clients' }))

    const button = screen.getByRole('button', { name: 'Refreshing…' })
    expect(button).toBeDisabled()
  })

  it('shows the failure banner when the sync fails', async () => {
    parentAnswers()
    const fetchMock = mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await screen.findByText('Cool Breeze HVAC')

    fetchMock.mockResolvedValueOnce(new Response('', { status: 502 }))
    await user.click(screen.getByRole('button', { name: 'Refresh clients' }))

    expect(
      await screen.findByText('Could not load billing data.', { exact: false }),
    ).toBeInTheDocument()
  })

  it('has no refresh button on a sub-account session, which has no list to refresh', async () => {
    parentAnswers()
    mockApi({ session: { ...agencySession, active_location: 'loc1', scope: 'location' } })

    render(<AdminPage />)

    await screen.findByText('Retell workspace')
    expect(screen.queryByRole('button', { name: /Refresh/ })).not.toBeInTheDocument()
  })

  // --- the client's own page ------------------------------------------------------------

  it('opens a client when its name is clicked', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    // The agent's own name, not the raw Retell id behind it.
    expect(screen.getByText('Cool Breeze front desk')).toBeInTheDocument()
    expect(screen.queryByText('agent_1')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '← All clients' })).toBeInTheDocument()
  })

  it('shows the money and the minutes on the client page', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(tile('AI minutes').getByText('200.00')).toBeInTheDocument()
    expect(tile('Call minutes').getByText('100.00')).toBeInTheDocument()
    expect(tile('Estimated').getByText('$55.00')).toBeInTheDocument()
    expect(tile('Cost').getByText('$22.20')).toBeInTheDocument()
    // $55.00 billed, less $18.00 to Retell and $4.20 to Twilio.
    expect(tile('Margin').getByText('$32.80')).toBeInTheDocument()
  })

  it('shows a call the way a person would read it, not the way Retell stores it', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const row = within(screen.getByText(`${MONTH_ABBREVIATION} 3, 2:45 PM`).closest('tr')!)
    // The customer's number on an inbound call is the one that rang in.
    expect(row.getByText('(213) 555-7788')).toBeInTheDocument()
    expect(row.getByText('inbound')).toBeInTheDocument()
    expect(row.getByText('3.70')).toBeInTheDocument()
    expect(row.getByText('$0.26')).toBeInTheDocument()
    expect(row.getByText('Billable')).toBeInTheDocument()
    // Nothing on this table is for a machine to read.
    expect(screen.queryByText('call_ok')).not.toBeInTheDocument()
    expect(screen.queryByText('agent_1', { selector: 'td' })).not.toBeInTheDocument()
  })

  /** A billable call on day `day` of this month, on a bill of that status or on none. */
  function callOn(day: number, billStatus: RecentCall['bill_status']): RecentCall {
    return {
      ...detail.recent_calls[0],
      call_id: `call_${day}_${billStatus ?? 'none'}`,
      started_at: `${THIS_MONTH}-${String(day).padStart(2, '0')}T14:45:00Z`,
      bill_status: billStatus,
    }
  }

  /** The row that call landed on, found the way a reader would: by when it happened. */
  function callRow(day: number) {
    return within(screen.getByText(`${MONTH_ABBREVIATION} ${day}, 2:45 PM`).closest('tr')!)
  }

  it('marks each call from the bill it is on, not from the month it is in', async () => {
    parentAnswers()
    mockApi({
      bill: {
        status: 'paid',
        total_cents: 47,
        failure_reason: '',
        retry_count: 0,
        max_retries: 3,
        next_retry_at: null,
      },
      calls: [
        callOn(28, 'paid'),
        callOn(29, 'paid'),
        // Made after the run had already charged the month: on no bill, and billable still.
        callOn(30, null),
        detail.recent_calls[1],
      ],
    })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(callRow(28).getByText('Paid')).toBeInTheDocument()
    expect(callRow(29).getByText('Paid')).toBeInTheDocument()
    expect(callRow(30).getByText('Billable')).toBeInTheDocument()
    // A call nobody can bill is no more billable for the month having been paid.
    expect(screen.getByText('Not connected')).toHaveAttribute(
      'title',
      'Call status is not_connected, not ended.',
    )
  })

  it('says a call is billed while the charge for it is still in the air', async () => {
    parentAnswers()
    mockApi({
      bill: {
        status: 'charging',
        total_cents: 5500,
        failure_reason: '',
        retry_count: 0,
        max_retries: 3,
        next_retry_at: null,
      },
      calls: [callOn(28, 'charging')],
    })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    // On a bill already - what Stripe makes of it is the headline tile's business, not the row's.
    expect(callRow(28).getByText('Billed')).toBeInTheDocument()
    expect(screen.queryByText('Billable')).not.toBeInTheDocument()
  })

  it('says a call on a bill being retried is billed, as for any charge still in flight', async () => {
    parentAnswers()
    mockApi({
      bill: {
        status: 'retrying',
        total_cents: 5500,
        failure_reason: 'card_declined',
        retry_count: 1,
        max_retries: 3,
        next_retry_at: '2026-11-03T15:00:00Z',
      },
      calls: [callOn(28, 'retrying')],
    })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(callRow(28).getByText('Billed')).toBeInTheDocument()
    expect(screen.queryByText('Billable')).not.toBeInTheDocument()
  })

  it('names in a few words why a call could not be billed', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const pill = screen.getByText('Not connected')
    expect(pill).toHaveAttribute('title', 'Call status is not_connected, not ended.')
    // A call Retell never timed has no duration or cost to show, and zero would claim it did.
    const row = within(pill.closest('tr')!)
    expect(row.getAllByText('—')).toHaveLength(2)
  })

  // --- mapping: the one thing an agency admin can write ---------------------------------

  /**
   * The admin API with the mapping endpoints live: the two pickers, and a PUT whose body is
   * captured so a test can assert what the page actually sent.
   */
  function mockMappingApi(
    options: { putStatus?: number; twilio?: boolean; keyStatus?: number } = {},
  ) {
    const { putStatus = 200, twilio = true, keyStatus = 200 } = options
    const saved: { body: Record<string, unknown> | null } = { body: null }
    const savedKey: { body: Record<string, unknown> | null } = { body: null }
    let isMapped = false

    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        const method = init?.method ?? 'GET'
        const json = (body: unknown, status = 200) =>
          Promise.resolve(
            new Response(JSON.stringify(body), {
              status,
              headers: { 'Content-Type': 'application/json' },
            }),
          )

        if (url.startsWith('/api/ghl/sso/session/')) return json(agencySession)
        if (url.startsWith('/api/rebilling/admin/twilio-subaccounts/')) {
          return json(twilioSubaccounts)
        }

        if (url.endsWith('/retell-key/')) {
          if (keyStatus !== 200) return json({ detail: 'Retell request failed.' }, keyStatus)
          if (method === 'PUT') {
            savedKey.body = JSON.parse(init!.body as string)
            isMapped = true
            return json({
              location_id: 'loc1',
              workspace_name: savedKey.body!.workspace_name,
              key_last4: String(savedKey.body!.api_key).slice(-4),
              ...workspaceAgents,
            })
          }
          return json(workspaceAgents)
        }

        if (method === 'PUT' && url.endsWith('/mapping/')) {
          if (putStatus !== 200) return json({}, putStatus)
          saved.body = JSON.parse(init!.body as string)
          isMapped = true
          return json({ location_id: 'loc1', client_name: 'Cool Breeze HVAC', ...saved.body })
        }

        if (url.startsWith('/api/rebilling/admin/clients/loc1/')) {
          return json({
            ...detail,
            twilio_billing_enabled: twilio,
            twilio_subaccount_sid:
              (saved.body?.twilio_subaccount_sid as string | undefined) ??
              detail.twilio_subaccount_sid,
            retell_workspace: savedKey.body
              ? {
                  ...detail.retell_workspace!,
                  workspace_name: String(savedKey.body.workspace_name ?? ''),
                  key_last4: String(savedKey.body.api_key).slice(-4),
                }
              : detail.retell_workspace,
          })
        }
        if (url.startsWith('/api/rebilling/admin/clients/loc-new/')) {
          return json({
            month: THIS_MONTH,
            location_id: 'loc-new',
            client_name: 'Polar Peak Heating',
            mapped: true,
            twilio_billing_enabled: twilio,
            active: true,
            billing_mode: 'auto_charge',
            retell_agent_ids: workspaceAgents.agents.map((a) => a.agent_id),
            retell_agents: workspaceAgents.agents,
            retell_workspace: {
              workspace_name: 'Polar Peak Heating',
              key_last4: 'ff21',
              status: 'active' as const,
              last_checked_at: `${THIS_MONTH}-20T03:00:00Z`,
              last_webhook_at: null,
              webhook_url: 'https://surpro.test/api/rebilling/webhooks/retell/loc-new/',
            },
            phone_numbers: [],
            twilio_subaccount_sid: '',
            owner_name: 'Rowan Blake',
            billing_email: 'rowan@polarpeak.test',
            stripe_customer_id: '',
            stripe_customer_name: '',
            billing_failure_reason: '',
            // Nobody has charged this one yet, so nothing has matched it in Stripe either.
            card: { status: 'no_customer', brand: '', last4: '', requested_at: null },
            rates: null,
            rate_history: [],
            default_rates: { voice_rate_cents: 25, call_rate_cents: 5 },
            bill: null,
            usage: {
              ...mapped.usage!,
              ai_minutes: '0.00',
              call_minutes: '0.00',
              estimated_bill_cents: null,
            },
            twilio_days: [],
            recent_calls: [],
          })
        }
        if (url.startsWith('/api/rebilling/admin/clients/')) {
          return json({
            scope: 'agency',
            month: THIS_MONTH,
            twilio_billing_enabled: twilio,
            clients: [mapped, { ...unmapped, mapped: isMapped, retell_connected: isMapped }],
          })
        }
        return Promise.resolve(new Response('', { status: 404 }))
      })

    return { fetchMock, saved, savedKey }
  }

  /** How many times one of the picker endpoints was asked for its list. */
  function listCalls(fetchMock: ReturnType<typeof mockMappingApi>['fetchMock'], path: string) {
    return fetchMock.mock.calls.filter(([url]) => String(url).startsWith(`/api${path}`)).length
  }

  it('leaves the Twilio list alone until its picker is opened', async () => {
    parentAnswers()
    const user = userEvent.setup()
    const { fetchMock } = mockMappingApi()

    render(<AdminPage />)
    await openClient(user)

    // Every visit to a client's page would otherwise cost a live call to Twilio, and most of
    // them never touch the mapping at all.
    expect(listCalls(fetchMock, '/rebilling/admin/twilio-subaccounts/')).toBe(0)

    await user.click(screen.getByRole('button', { name: 'ACchild' }))

    await screen.findByText('Polar Peak Heating')
    expect(listCalls(fetchMock, '/rebilling/admin/twilio-subaccounts/')).toBe(1)
  })

  // --- the client's own Retell workspace key ----------------------------------------------

  it('shows the key by its last four and the webhook URL to paste into Retell', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(screen.getByText('••••3a9f')).toBeInTheDocument()
    expect(
      screen.getByText('https://surpro.test/api/rebilling/webhooks/retell/loc1/'),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy the webhook URL' })).toBeInTheDocument()
    expect(screen.getByText('Active')).toBeInTheDocument()
  })

  it('tells an admin where in Retell to find the key, badge and all', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    // The one instruction that cannot be enforced in code: any other key of the same
    // workspace passes the test below and then fails every webhook signature.
    expect(screen.getByText('Settings → API Keys')).toBeInTheDocument()
    expect(screen.getByText('copy the key with the webhook badge')).toBeInTheDocument()
  })

  it('says a client has no key yet rather than leaving the row blank', async () => {
    parentAnswers()
    mockApi({ detail: { retell_workspace: null } })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(screen.getByText('No key')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy the webhook URL' })).not.toBeInTheDocument()
  })

  it('flags a saved key that has never been heard from, which is the badge trap', async () => {
    parentAnswers()
    mockApi({
      detail: { retell_workspace: { ...detail.retell_workspace!, last_webhook_at: null } },
    })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(
      screen.getByText(/probably not the one with the webhook badge/),
    ).toBeInTheDocument()
  })

  it('says when the nightly audit could not read the workspace with this key', async () => {
    parentAnswers()
    mockApi({
      detail: {
        retell_workspace: { ...detail.retell_workspace!, status: 'key_invalid' },
      },
    })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(screen.getByText('Key not working')).toBeInTheDocument()
    expect(screen.getByText(/Calls are not being recorded/)).toBeInTheDocument()
  })

  it('shows the agents a tested key found, and asks for them to be checked', async () => {
    parentAnswers()
    const user = userEvent.setup()
    mockMappingApi()

    render(<AdminPage />)
    await openClient(user)

    await user.type(screen.getByLabelText('Retell workspace API key'), 'key_new_abcd')
    await user.click(screen.getByRole('button', { name: 'Test' }))

    // The names are the whole point: another client's key would authenticate here too.
    expect(await screen.findByText(/This key can see 2 agents/)).toBeInTheDocument()
    expect(
      screen.getByText(/Cool Breeze front desk, Cool Breeze after hours/),
    ).toBeInTheDocument()
    expect(screen.getByText(/before saving/)).toBeInTheDocument()
  })

  it('will not save a key until it has been tested', async () => {
    parentAnswers()
    const user = userEvent.setup()
    mockMappingApi()

    render(<AdminPage />)
    await openClient(user)

    expect(screen.getByRole('button', { name: 'Save key' })).toBeDisabled()

    await user.type(screen.getByLabelText('Retell workspace API key'), 'key_new_abcd')
    expect(screen.getByRole('button', { name: 'Save key' })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Test' }))
    await screen.findByText(/This key can see 2 agents/)

    expect(screen.getByRole('button', { name: 'Save key' })).toBeEnabled()
  })

  it('makes an edited key be tested again before it can be saved', async () => {
    parentAnswers()
    const user = userEvent.setup()
    mockMappingApi()

    render(<AdminPage />)
    await openClient(user)

    const field = screen.getByLabelText('Retell workspace API key')
    await user.type(field, 'key_new_abcd')
    await user.click(screen.getByRole('button', { name: 'Test' }))
    await screen.findByText(/This key can see 2 agents/)

    // A confirmation must never belong to a different key than the one about to be saved.
    await user.type(field, 'X')

    expect(screen.getByRole('button', { name: 'Save key' })).toBeDisabled()
    expect(screen.queryByText(/This key can see 2 agents/)).not.toBeInTheDocument()
  })

  it('sends the tested key to the backend and shows its last four afterwards', async () => {
    parentAnswers()
    const user = userEvent.setup()
    const { savedKey } = mockMappingApi()

    render(<AdminPage />)
    await openClient(user)

    await user.type(screen.getByLabelText('Retell workspace API key'), 'key_new_abcd')
    await user.clear(screen.getByLabelText('Retell workspace name'))
    await user.type(screen.getByLabelText('Retell workspace name'), 'Cool Breeze')
    await user.click(screen.getByRole('button', { name: 'Test' }))
    await screen.findByText(/This key can see 2 agents/)
    await user.click(screen.getByRole('button', { name: 'Save key' }))

    await waitFor(() =>
      expect(savedKey.body).toEqual({ api_key: 'key_new_abcd', workspace_name: 'Cool Breeze' }),
    )
    // Read back from the server rather than assumed, and the field is cleared behind it.
    expect(await screen.findByText('••••abcd')).toBeInTheDocument()
    expect(screen.getByLabelText('Retell workspace API key')).toHaveValue('')
  })

  it('says so when Retell refuses the key, and keeps it out of the database', async () => {
    parentAnswers()
    const user = userEvent.setup()
    const { savedKey } = mockMappingApi({ keyStatus: 502 })

    render(<AdminPage />)
    await openClient(user)

    await user.type(screen.getByLabelText('Retell workspace API key'), 'key_wrong')
    await user.click(screen.getByRole('button', { name: 'Test' }))

    expect(
      await screen.findByText(/Retell would not accept this key/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save key' })).toBeDisabled()
    expect(savedKey.body).toBeNull()
  })

  it('shows a sub-account its workspace status and nothing it could change', async () => {
    parentAnswers()
    mockApi({ session: { ...agencySession, active_location: 'loc1', scope: 'location' } })

    render(<AdminPage />)
    await screen.findByText('Retell workspace')

    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.queryByLabelText('Retell workspace API key')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Test' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save key' })).not.toBeInTheDocument()
  })

  it('shows who pays and the Stripe customer matched to them, with nothing to edit', async () => {
    parentAnswers()
    const user = userEvent.setup()
    mockMappingApi()

    render(<AdminPage />)
    await openClient(user)

    // Read from the sub-account's GoHighLevel owner, matched in Stripe by their email.
    expect(await screen.findByText('Dana Reyes')).toBeInTheDocument()
    expect(screen.getByText('dana@coolbreeze.test')).toBeInTheDocument()
    // The Stripe customer's own name, never the raw id behind it.
    expect(screen.getByText('Cool Breeze HVAC LLC')).toBeInTheDocument()
    expect(screen.queryByText('cus_coolbreeze')).not.toBeInTheDocument()
    // No picker and no text box: changing who pays means changing it in GoHighLevel.
    expect(screen.queryByRole('button', { name: /Stripe customer/i })).not.toBeInTheDocument()
  })

  it('falls back to the sub-account name when GoHighLevel has no owner on file', async () => {
    parentAnswers()
    mockApi({ detail: { owner_name: '' } })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    // Still a name, never blank and never a reason to show the id instead - the client's own
    // name, on the billing contact card's "Sub-account" row specifically.
    const subAccountRow = (await screen.findByText('Sub-account')).nextElementSibling
    expect(subAccountRow).toHaveTextContent('Cool Breeze HVAC')
    expect(screen.queryByText('Not set in GoHighLevel')).not.toBeInTheDocument()
  })

  it('says so when Stripe has no name on file for an otherwise matched customer', async () => {
    parentAnswers()
    mockApi({ detail: { stripe_customer_name: '' } })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(await screen.findByText('No name on file in Stripe')).toBeInTheDocument()
    expect(screen.queryByText('cus_coolbreeze')).not.toBeInTheDocument()
  })

  it('shows a sub-account its mapped agents by name, same as the agency admin does', async () => {
    parentAnswers()
    mockApi({ session: { ...agencySession, active_location: 'loc1', scope: 'location' } })

    render(<AdminPage />)

    expect(await screen.findByText('Cool Breeze front desk')).toBeInTheDocument()
    expect(screen.queryByText('agent_1')).not.toBeInTheDocument()
  })

  it('picks a Twilio subaccount by name and saves its SID', async () => {
    parentAnswers()
    const user = userEvent.setup()
    const { saved } = mockMappingApi()

    render(<AdminPage />)
    await openClient(user)

    // The trigger reads the SID this client is on today, not an empty text box.
    await user.click(screen.getByRole('button', { name: 'ACchild' }))
    await user.click(await screen.findByRole('button', { name: /Polar Peak Heating/ }))
    await user.click(screen.getByRole('button', { name: 'Save mapping' }))

    await waitFor(() =>
      expect(saved.body).toEqual({
        twilio_subaccount_sid: NEW_SID,
        phone_numbers: ['+15551234567'],
      }),
    )
    // The page reads back what it just saved rather than trusting its own optimism.
    expect(await screen.findByRole('button', { name: NEW_SID })).toBeInTheDocument()
  })

  it('can take a client off Twilio billing from the same list', async () => {
    parentAnswers()
    const user = userEvent.setup()
    const { saved } = mockMappingApi()

    render(<AdminPage />)
    await openClient(user)

    await user.click(screen.getByRole('button', { name: 'ACchild' }))
    await user.click(await screen.findByRole('button', { name: 'No Twilio subaccount' }))
    await user.click(screen.getByRole('button', { name: 'Save mapping' }))

    await waitFor(() => expect(saved.body?.twilio_subaccount_sid).toBe(''))
  })

  it('says so when the Twilio list cannot be loaded, and retries on the next open', async () => {
    parentAnswers()
    const user = userEvent.setup()
    let asked = false

    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      const json = (body: unknown, status = 200) =>
        Promise.resolve(
          new Response(JSON.stringify(body), {
            status,
            headers: { 'Content-Type': 'application/json' },
          }),
        )

      if (url.startsWith('/api/ghl/sso/session/')) return json(agencySession)
      if (url.startsWith('/api/rebilling/admin/twilio-subaccounts/')) {
        if (asked) return json(twilioSubaccounts)
        asked = true
        return json({ detail: 'Twilio request failed.' }, 502)
      }
      if (url.startsWith('/api/rebilling/admin/clients/loc1/')) return json(detail)
      if (url.startsWith('/api/rebilling/admin/clients/')) {
        return json({
          scope: 'agency',
          month: THIS_MONTH,
          twilio_billing_enabled: true,
          clients: [mapped, unmapped],
        })
      }
      return Promise.resolve(new Response('', { status: 404 }))
    })

    render(<AdminPage />)
    await openClient(user)
    await user.click(screen.getByRole('button', { name: 'ACchild' }))

    expect(
      await screen.findByText('Could not load the Twilio subaccounts.', { exact: false }),
    ).toBeInTheDocument()

    // Opening it again asks Twilio once more rather than stranding the admin on the error.
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'ACchild' }))

    expect(await screen.findByText('Polar Peak Heating')).toBeInTheDocument()
  })

  it('sets a previously unmapped client up with a key, which then opens as a full page', async () => {
    parentAnswers()
    const user = userEvent.setup()
    mockMappingApi()

    render(<AdminPage />)
    await user.click(await screen.findByRole('button', { name: 'Polar Peak Heating' }))
    await screen.findByText('Not set up for billing yet.', { exact: false })

    await user.type(screen.getByLabelText('Retell workspace API key'), 'key_polar_ff21')
    await user.click(screen.getByRole('button', { name: 'Test' }))
    await screen.findByText(/This key can see 2 agents/)
    await user.click(screen.getByRole('button', { name: 'Save key' }))

    // Now a real client page, not the setup card - and the agents came off the workspace,
    // which is the only place they ever come from.
    expect(await screen.findByText('Recent AI calls')).toBeInTheDocument()
    expect(
      screen.queryByText('Not set up for billing yet.', { exact: false }),
    ).not.toBeInTheDocument()
  })

  it('shows an error and keeps the choices when a save is rejected', async () => {
    parentAnswers()
    const user = userEvent.setup()
    mockMappingApi({ putStatus: 400 })

    render(<AdminPage />)
    await openClient(user)

    const phoneInput = screen.getByLabelText('Add a phone number')
    await user.type(phoneInput, '12')
    await user.click(
      within(phoneInput.closest('.chip-add') as HTMLElement).getByRole('button', { name: 'Add' }),
    )
    await user.click(screen.getByRole('button', { name: 'Save mapping' }))

    expect(
      await screen.findByText('Check the phone numbers below.', { exact: false }),
    ).toBeInTheDocument()
    // Nothing was thrown away: the rejected number is still there to be corrected.
    expect(screen.getByText('12')).toBeInTheDocument()
  })

  it('opens straight into the one client when GHL says this is a sub-account', async () => {
    parentAnswers()
    mockApi({ session: { ...agencySession, active_location: 'loc1', scope: 'location' } })

    render(<AdminPage />)

    expect(await screen.findByText('Retell workspace')).toBeInTheDocument()
    // No way back to a list that this session is not allowed to see.
    expect(screen.queryByRole('button', { name: '← All clients' })).not.toBeInTheDocument()
    // The period filter is still there, because this client's own figures still need one.
    expect(monthSelect()).toBeInTheDocument()
  })

  // --- the period filter, on the client's page -------------------------------------------

  it('defaults the period to the current billing month', async () => {
    parentAnswers()
    const fetchMock = mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(monthSelect().value).toBe(THIS_MONTH)
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).startsWith(`/api/rebilling/admin/clients/loc1/?month=${THIS_MONTH}`),
      ),
    ).toBe(true)
  })

  it('refetches the open client when the period changes', async () => {
    parentAnswers()
    const quietMonth: ClientSummary = {
      ...mapped,
      usage: { ...mapped.usage!, ai_minutes: '7.00' },
    }
    const fetchMock = mockApi({ perMonth: { [LAST_MONTH]: quietMonth } })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    await user.selectOptions(monthSelect(), LAST_MONTH)

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).startsWith(`/api/rebilling/admin/clients/loc1/?month=${LAST_MONTH}`),
        ),
      ).toBe(true),
    )
    await waitFor(() => expect(tile('AI minutes').getByText('7.00')).toBeInTheDocument())
  })

  it('steps a month at a time with the arrows', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    await user.click(screen.getByLabelText('Previous month'))
    await waitFor(() => expect(monthSelect().value).toBe(LAST_MONTH))

    await waitFor(() => expect(screen.getByLabelText('Next month')).toBeEnabled())
    await user.click(screen.getByLabelText('Next month'))
    await waitFor(() => expect(monthSelect().value).toBe(THIS_MONTH))
  })

  it('will not step past the current month, because nothing is billed in the future', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(screen.getByLabelText('Next month')).toBeDisabled()
  })

  it('never shows one period under another period heading', async () => {
    parentAnswers()
    const fetchMock = mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)
    expect(tile('AI minutes').getByText('200.00')).toBeInTheDocument()

    // The request for last month never resolves, so the page must keep waiting rather than
    // leave this month's numbers on screen under the new label.
    fetchMock.mockImplementation(() => new Promise(() => {}))
    await user.selectOptions(monthSelect(), LAST_MONTH)

    expect(await screen.findByLabelText('Loading client')).toBeInTheDocument()
    expect(screen.queryByText('200.00')).not.toBeInTheDocument()
  })

  // --- trying a different rate ------------------------------------------------------------

  it('re-estimates the bill and the margin as a rate is typed', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const aiRate = screen.getByLabelText('AI minute rate')
    await user.clear(aiRate)
    await user.type(aiRate, '0.50')

    // 200 AI minutes at 50c, plus 100 call minutes at 5c.
    await waitFor(() => expect(tile('Estimated').getByText('$105.00')).toBeInTheDocument())
    expect(tile('Margin').getByText('$82.80')).toBeInTheDocument()
    expect(screen.getByText(/not saved/)).toBeInTheDocument()
  })

  it('puts the client back on the system defaults, ready to save', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const callRate = screen.getByLabelText('Call minute rate')
    await user.clear(callRate)
    await user.type(callRate, '0.20')
    await waitFor(() => expect(tile('Estimated').getByText('$70.00')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Reset to defaults' }))

    // The backend's own defaults, which it sends with the client - the page has no copy.
    expect(screen.getByLabelText('AI minute rate')).toHaveValue('0.25')
    expect(screen.getByLabelText('Call minute rate')).toHaveValue('0.05')
    expect(tile('Estimated').getByText('$55.00')).toBeInTheDocument()
  })

  // --- saving a rate -----------------------------------------------------------------------

  /**
   * The admin API with the rates endpoint live.
   *
   * A PUT answers with the client's whole refreshed detail, as the real endpoint does, and is
   * remembered - so a later detail read answers with the saved rates too. That is what makes
   * these tests prove the page ends up showing what the server holds rather than its own
   * optimism about it.
   */
  function mockRatesApi(
    options: { putStatus?: number; twilio?: boolean; detail?: Partial<ClientDetail> } = {},
  ) {
    const { putStatus = 200, twilio = true } = options
    const saved: { body: RateFields | null } = { body: null }
    let current: ClientDetail = { ...detail, twilio_billing_enabled: twilio, ...options.detail }

    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        const method = init?.method ?? 'GET'
        const month = new URL(url, 'http://test').searchParams.get('month') ?? THIS_MONTH
        const json = (body: unknown, status = 200) =>
          Promise.resolve(
            new Response(JSON.stringify(body), {
              status,
              headers: { 'Content-Type': 'application/json' },
            }),
          )

        if (url.startsWith('/api/ghl/sso/session/')) return json(agencySession)

        if (method === 'PUT' && url.includes('/rates/')) {
          if (putStatus !== 200) return json({}, putStatus)
          saved.body = JSON.parse(init!.body as string)
          current = atRates(saved.body!, month, twilio)
          return json(current)
        }

        if (url.startsWith('/api/rebilling/admin/clients/loc1/')) return json({ ...current, month })

        if (url.startsWith('/api/rebilling/admin/clients/')) {
          const body: ClientsResponse = {
            scope: 'agency',
            month,
            twilio_billing_enabled: twilio,
            clients: [mapped, unmapped],
          }
          return json(body)
        }
        return Promise.resolve(new Response('', { status: 404 }))
      })

    return { fetchMock, saved }
  }

  /** What the backend answers after a save: a new rate from today, and the estimate worked out
   * again from it. */
  function atRates(rates: RateFields, month: string, twilio = true): ClientDetail {
    const today = new Date().toISOString().slice(0, 10)
    return {
      ...detail,
      month,
      twilio_billing_enabled: twilio,
      rates: { ...rates, start_date: today },
      rate_history: [{ ...rates, start_date: today }, ...detail.rate_history],
      usage: {
        ...detail.usage,
        // Retell AI minutes only once Twilio rebilling is paused, as the real endpoint does.
        estimated_bill_cents: Math.round(
          Number(detail.usage.ai_minutes) * rates.voice_rate_cents +
            (twilio ? Number(detail.usage.call_minutes) * rates.call_rate_cents : 0),
        ),
      },
    }
  }

  it('saves a changed rate as whole cents', async () => {
    parentAnswers()
    const { saved } = mockRatesApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const aiRate = screen.getByLabelText('AI minute rate')
    await user.clear(aiRate)
    await user.type(aiRate, '0.50')
    await user.click(screen.getByRole('button', { name: 'Save rates' }))

    // Cents, never dollars: the same integers the bill and the Stripe invoice are made of.
    await waitFor(() => expect(saved.body).toEqual({ voice_rate_cents: 50, call_rate_cents: 5 }))
  })

  it('shows the server figures at the saved rate, and stops calling it unsaved', async () => {
    parentAnswers()
    mockRatesApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const aiRate = screen.getByLabelText('AI minute rate')
    await user.clear(aiRate)
    await user.type(aiRate, '0.50')
    expect(screen.getByText(/not saved/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Save rates' }))

    // 200 AI minutes at 50c plus 100 call minutes at 5c, as the backend worked it out.
    await waitFor(() => expect(tile('Estimated').getByText('$105.00')).toBeInTheDocument())
    expect(screen.queryByText(/not saved/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('AI minute rate')).toHaveValue('0.50')
  })

  it('saves the defaults after a reset, which become the rates the client is on', async () => {
    parentAnswers()
    const { saved } = mockRatesApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const aiRate = screen.getByLabelText('AI minute rate')
    await user.clear(aiRate)
    await user.type(aiRate, '0.90')
    await user.click(screen.getByRole('button', { name: 'Reset to defaults' }))
    await user.click(screen.getByRole('button', { name: 'Save rates' }))

    await waitFor(() => expect(saved.body).toEqual({ voice_rate_cents: 25, call_rate_cents: 5 }))
    expect(screen.getByLabelText('AI minute rate')).toHaveValue('0.25')
  })

  it('keeps the typed rate on screen and says so when the save fails', async () => {
    parentAnswers()
    mockRatesApi({ putStatus: 500 })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const aiRate = screen.getByLabelText('AI minute rate')
    await user.clear(aiRate)
    await user.type(aiRate, '0.50')
    await user.click(screen.getByRole('button', { name: 'Save rates' }))

    expect(await screen.findByText('Could not save the rates. Try again.')).toBeInTheDocument()
    // Nothing is thrown away: the rate is still there to try again with.
    expect(screen.getByLabelText('AI minute rate')).toHaveValue('0.50')
  })

  it('shows a sub-account its rates without letting it change them', async () => {
    parentAnswers()
    mockApi({ session: { ...agencySession, active_location: 'loc1', scope: 'location' } })

    render(<AdminPage />)
    await screen.findByText('Retell workspace')

    // Read-only: a client sees what it is charged, and nothing to change it with.
    expect(screen.getByText('$0.25')).toBeInTheDocument()
    expect(screen.queryByLabelText('AI minute rate')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save rates' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reset to defaults' })).not.toBeInTheDocument()
  })

  // --- a month that has already been billed -------------------------------------------------

  it('reports a paid bill instead of still calling the month an estimate', async () => {
    parentAnswers()
    mockApi({ bill: {
      status: 'paid',
      total_cents: 47,
      failure_reason: '',
      retry_count: 0,
      max_retries: 3,
      next_retry_at: null,
    } })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(tile('Paid').getByText('$0.47')).toBeInTheDocument()
    expect(screen.queryByText('Estimated', { selector: '.tile-label' })).not.toBeInTheDocument()
    expect(screen.getByText(/Billed and paid in full/)).toBeInTheDocument()
  })

  it('says a bill is still waiting on Stripe', async () => {
    parentAnswers()
    mockApi({ bill: {
      status: 'charging',
      total_cents: 5500,
      failure_reason: '',
      retry_count: 0,
      max_retries: 3,
      next_retry_at: null,
    } })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(tile('Charging').getByText('$55.00')).toBeInTheDocument()
    expect(screen.getByText(/Waiting for Stripe to confirm/)).toBeInTheDocument()
  })

  it('shows why a charge failed, in Stripe own words', async () => {
    parentAnswers()
    mockApi({
      bill: {
        status: 'failed',
        total_cents: 5500,
        failure_reason: 'authentication_required',
        retry_count: 0,
        max_retries: 3,
        next_retry_at: null,
      },
    })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(tile('Failed').getByText('$55.00')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('authentication_required')
  })

  it('shows a charge being retried as In Retry, with the attempt and when the next is due', async () => {
    parentAnswers()
    mockApi({
      bill: {
        status: 'retrying',
        total_cents: 5500,
        failure_reason: 'card_declined',
        retry_count: 2,
        max_retries: 3,
        next_retry_at: '2026-11-03T15:00:00Z',
      },
    })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(tile('In Retry').getByText('$55.00')).toBeInTheDocument()
    expect(screen.getByText('In Retry', { selector: '.tile-label' })).toHaveClass('pill', 'warn')
    // Not a permanent failure: no Failed label, and the reason sits in a notice, not an error.
    expect(screen.queryByText('Failed', { selector: '.tile-label' })).not.toBeInTheDocument()
    expect(screen.getByText('Retry 2 of 3')).toBeInTheDocument()
    expect(screen.getByText(/Next retry Nov 3, 3:00 PM UTC/)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveClass('warn')
    expect(screen.getByRole('alert')).toHaveTextContent('card_declined')
  })

  it('shows a charge whose retries are spent as a red Failed, for someone to act on', async () => {
    parentAnswers()
    mockApi({
      bill: {
        status: 'failed',
        total_cents: 5500,
        failure_reason: 'card_declined',
        retry_count: 3,
        max_retries: 3,
        next_retry_at: null,
      },
    })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(tile('Failed').getByText('$55.00')).toBeInTheDocument()
    expect(screen.getByText('Failed', { selector: '.tile-label' })).toHaveClass('pill', 'err')
    expect(screen.queryByText('In Retry')).not.toBeInTheDocument()
    expect(screen.queryByText(/Retry \d of \d/)).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveClass('error')
  })

  it('shows a bill paid after retries as Paid, with no retry wording left over', async () => {
    parentAnswers()
    mockApi({
      bill: {
        status: 'paid',
        total_cents: 5500,
        failure_reason: '',
        retry_count: 1,
        max_retries: 3,
        next_retry_at: null,
      },
    })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(tile('Paid').getByText('$55.00')).toBeInTheDocument()
    expect(screen.getByText('Paid', { selector: '.tile-label' })).not.toHaveClass('pill')
    expect(screen.queryByText(/Retry \d of \d/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Next retry/)).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows why a bill that never reached Stripe could not be charged', async () => {
    parentAnswers()
    mockApi({
      bill: {
        status: 'uncharged',
        total_cents: 5500,
        failure_reason: 'No Stripe customer found for billing email: dana@coolbreeze.test',
        retry_count: 0,
        max_retries: 3,
        next_retry_at: null,
      },
    })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    // `uncharged` with a reason is the state nobody can act on without being told about it.
    expect(tile('Uncharged').getByText('$55.00')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('No Stripe customer found')
  })

  it('leaves a billed month alone when a rate is typed', async () => {
    parentAnswers()
    mockApi({ bill: {
      status: 'paid',
      total_cents: 47,
      failure_reason: '',
      retry_count: 0,
      max_retries: 3,
      next_retry_at: null,
    } })
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const aiRate = screen.getByLabelText('AI minute rate')
    await user.clear(aiRate)
    await user.type(aiRate, '0.50')

    // The bill was worked out from the rate in effect on each call's own day. Nothing typed
    // now can move what the client was charged.
    await waitFor(() => expect(screen.getByLabelText('AI minute rate')).toHaveValue('0.50'))
    expect(tile('Paid').getByText('$0.47')).toBeInTheDocument()
  })

  it('starts a new period from the rate the client is actually on', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const aiRate = screen.getByLabelText('AI minute rate')
    await user.clear(aiRate)
    await user.type(aiRate, '0.90')
    await waitFor(() => expect(screen.getByLabelText('AI minute rate')).toHaveValue('0.90'))

    await user.selectOptions(monthSelect(), LAST_MONTH)

    // A what-if rate is for the period it was typed in. Carrying it over would quietly
    // misprice the next one.
    await waitFor(() => expect(screen.getByLabelText('AI minute rate')).toHaveValue('0.25'))
  })

  // --- no GoHighLevel to ask -------------------------------------------------------------

  it('points to the sample preview instead of showing demo data when there is nothing to ask', async () => {
    // Framed, but nothing answers: the handshake times out with no live session to fall back on.
    parentIgnores()
    const fetchMock = mockApi()

    render(<AdminPage />)
    await vi.advanceTimersByTimeAsync(10_000)

    const link = await screen.findByRole('link', { name: /See a sample with demo data/ })
    expect(link).toHaveAttribute('href', '/admin/preview')
    // This route never shows sample data itself, and never calls the authenticated API either.
    expect(screen.queryByText('Cool Breeze HVAC')).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('shows the same message immediately when the page is not framed at all', async () => {
    // window.parent === window when opened directly in a browser, so there is nothing to wait
    // for and no timeout to sit through.
    const fetchMock = mockApi()

    render(<AdminPage />)

    expect(
      await screen.findByText('Open this page from inside GoHighLevel', { exact: false }),
    ).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  // --- an unmapped sub-account's own page -------------------------------------------------

  it('shows a friendly notice instead of a 404 for an unmapped sub-account', async () => {
    parentAnswers()
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      const json = (body: unknown) =>
        Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        )

      if (url.startsWith('/api/ghl/sso/session/')) {
        return json({ ...agencySession, active_location: 'loc-new', scope: 'location' })
      }
      if (url.startsWith('/api/rebilling/admin/clients/loc-new/')) {
        return json({
          month: THIS_MONTH,
          location_id: 'loc-new',
          client_name: 'Polar Peak Heating',
          mapped: false,
          twilio_billing_enabled: true,
        })
      }
      if (url.startsWith('/api/rebilling/admin/clients/')) {
        return json({
          scope: 'location',
          month: THIS_MONTH,
          twilio_billing_enabled: true,
          clients: [],
        })
      }
      return Promise.resolve(new Response('', { status: 404 }))
    })

    render(<AdminPage />)

    expect(
      await screen.findByText("Voice billing isn't set up for this client yet."),
    ).toBeInTheDocument()
    expect(screen.getByText('Polar Peak Heating')).toBeInTheDocument()
    // Not an error: no failure banner, and still the client's own page, not a list.
    expect(screen.queryByText('Could not load billing data.', { exact: false })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '← All clients' })).not.toBeInTheDocument()
  })

  // --- failures ------------------------------------------------------------------------

  it('says so when GoHighLevel sends a session the backend refuses', async () => {
    parentAnswers()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 401 }))

    render(<AdminPage />)

    expect(await screen.findByText('could not accept', { exact: false })).toBeInTheDocument()
    // A refused session is a misconfiguration, not a reason to show samples.
    expect(screen.queryByText('Standalone preview.', { exact: false })).not.toBeInTheDocument()
  })

  it('reports a failed load rather than rendering an empty page', async () => {
    parentAnswers()
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/ghl/sso/session/')) {
        return Promise.resolve(
          new Response(JSON.stringify(agencySession), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
      }
      return Promise.resolve(new Response('', { status: 500 }))
    })

    render(<AdminPage />)

    await waitFor(() =>
      expect(screen.getByText('Could not load billing data.', { exact: false })).toBeInTheDocument(),
    )
  })

  // --- Twilio rebilling paused ------------------------------------------------------------

  /**
   * GoHighLevel already rebills Twilio for these sub-accounts, so billing it here too would
   * charge the client twice. The backend reports that with `twilio_billing_enabled: false` and
   * leaves Twilio out of its own estimate; the page has to leave it out of everything it shows
   * and everything it sends. This is a pause, not a deletion - the last case here, and every
   * test above it in this file, runs with the flag on and proves the Twilio half still works.
   */
  describe('while Twilio rebilling is paused', () => {
    /** The usage the backend sends while paused: the estimate is Retell AI minutes only. */
    const pausedUsage = { ...mapped.usage!, estimated_bill_cents: 5000 }
    const pausedMonth = { [THIS_MONTH]: { ...mapped, usage: pausedUsage } }

    /** A day of Twilio usage, which is still ingested and still sent - just never shown. */
    const twilioDay = {
      usage_date: `${THIS_MONTH}-04`,
      category: 'calls',
      call_count: 9,
      usage_minutes: '31.00',
      twilio_price_cents: 40,
      as_of: `${THIS_MONTH}-05T03:30:00Z`,
    }

    it('drops the call minutes tile and counts Retell cost only', async () => {
      parentAnswers()
      mockApi({ twilio: false, perMonth: pausedMonth })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)

      expect(screen.queryByText('Call minutes', { selector: '.tile-label' })).not.toBeInTheDocument()
      expect(tile('AI minutes').getByText('200.00')).toBeInTheDocument()
      // 200 AI minutes at 25c, with nothing for the 100 call minutes.
      expect(tile('Estimated').getByText('$50.00')).toBeInTheDocument()
      // Twilio's own $4.20 is GoHighLevel's to rebill, so it is neither a cost nor a margin here.
      expect(tile('Cost').getByText('$18.00')).toBeInTheDocument()
      expect(tile('Margin').getByText('$32.00')).toBeInTheDocument()
    })

    it('re-estimates from the AI rate alone, matching what the server would say', async () => {
      parentAnswers()
      mockApi({ twilio: false, perMonth: pausedMonth })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)

      expect(screen.queryByLabelText('Call minute rate')).not.toBeInTheDocument()

      const aiRate = screen.getByLabelText('AI minute rate')
      await user.clear(aiRate)
      await user.type(aiRate, '0.50')

      // 200 AI minutes at 50c and no call-minute term at all.
      await waitFor(() => expect(tile('Estimated').getByText('$100.00')).toBeInTheDocument())
      expect(tile('Margin').getByText('$82.00')).toBeInTheDocument()
    })

    it('hides the Twilio calls by day table, its staleness pill and its as-of note', async () => {
      parentAnswers()
      mockApi({
        twilio: false,
        perMonth: pausedMonth,
        detail: { twilio_days: [twilioDay], usage: { ...pausedUsage, twilio_stale: true } },
      })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)

      expect(screen.queryByText('Twilio calls by day')).not.toBeInTheDocument()
      expect(screen.queryByText('Stale')).not.toBeInTheDocument()
      expect(screen.queryByText(/Current through/)).not.toBeInTheDocument()
      // And nothing anywhere else on the page names it either - the recent calls table is
      // Retell's own, and the rates and mapping cards have dropped their Twilio rows.
      expect(screen.queryByText(/Twilio/)).not.toBeInTheDocument()
      // The Retell half is untouched.
      expect(screen.getByText('Recent AI calls')).toBeInTheDocument()
      expect(screen.getByText('(213) 555-7788')).toBeInTheDocument()
    })

    it('drops the whole Twilio card, which has nothing left in it', async () => {
      parentAnswers()
      const user = userEvent.setup()
      mockMappingApi({ twilio: false })

      render(<AdminPage />)
      await openClient(user)

      // With Retell on its own card there is nothing else in there, so it goes rather than
      // sitting empty. Retell's own card is untouched.
      expect(screen.queryByText('Twilio')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'ACchild' })).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Add a phone number')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Save mapping' })).not.toBeInTheDocument()
      expect(screen.getByText('Retell workspace')).toBeInTheDocument()
    })

    it('asks only for the Retell key when setting a client up for the first time', async () => {
      parentAnswers()
      const user = userEvent.setup()
      mockMappingApi({ twilio: false })

      render(<AdminPage />)
      await user.click(await screen.findByRole('button', { name: 'Polar Peak Heating' }))

      expect(
        await screen.findByText('Not set up for billing yet.', { exact: false }),
      ).toBeInTheDocument()
      expect(screen.getByLabelText('Retell workspace API key')).toBeInTheDocument()
      expect(screen.queryByLabelText('Add a phone number')).not.toBeInTheDocument()
      expect(
        screen.queryByRole('button', { name: 'Choose a Twilio subaccount' }),
      ).not.toBeInTheDocument()
    })

    it('shows a sub-account neither its phone numbers nor its Twilio subaccount', async () => {
      parentAnswers()
      mockApi({
        twilio: false,
        perMonth: pausedMonth,
        session: { ...agencySession, active_location: 'loc1', scope: 'location' },
      })

      render(<AdminPage />)
      await screen.findByText('Retell workspace')

      // The Retell card keeps the one thing that is billed here.
      expect(screen.getByText('Agents')).toBeInTheDocument()
      expect(screen.getByText('Cool Breeze front desk')).toBeInTheDocument()
      expect(screen.queryByText('Phone numbers')).not.toBeInTheDocument()
      expect(screen.queryByText('Twilio subaccount')).not.toBeInTheDocument()
      expect(screen.queryByText('+15551234567')).not.toBeInTheDocument()
      // And the read-only rates lose their call-minute row with it.
      expect(screen.queryByText('Call minute')).not.toBeInTheDocument()
    })

    it('sends the stored call rate back unchanged when the field is hidden', async () => {
      parentAnswers()
      const { saved } = mockRatesApi({ twilio: false })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)

      const aiRate = screen.getByLabelText('AI minute rate')
      await user.clear(aiRate)
      await user.type(aiRate, '0.50')
      await user.click(screen.getByRole('button', { name: 'Save rates' }))

      // The endpoint requires both - they share one RateHistory row - so the stored 5c goes
      // back as it is. A zero here would quietly reprice every Twilio minute to nothing.
      await waitFor(() => expect(saved.body).toEqual({ voice_rate_cents: 50, call_rate_cents: 5 }))
    })

    it('leaves the stored call rate alone when the rates are reset to defaults', async () => {
      parentAnswers()
      const { saved } = mockRatesApi({
        twilio: false,
        // A client on a call rate of its own, which the defaults would otherwise overwrite.
        detail: {
          rates: { voice_rate_cents: 40, call_rate_cents: 9, start_date: `${THIS_MONTH}-01` },
        },
      })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)
      expect(screen.getByLabelText('AI minute rate')).toHaveValue('0.40')

      await user.click(screen.getByRole('button', { name: 'Reset to defaults' }))
      expect(screen.getByLabelText('AI minute rate')).toHaveValue('0.25')
      await user.click(screen.getByRole('button', { name: 'Save rates' }))

      // Only the AI rate is reset: resetting a rate nobody can see is not what was asked for.
      await waitFor(() => expect(saved.body).toEqual({ voice_rate_cents: 25, call_rate_cents: 9 }))
    })

    it('stops the list pill from mentioning Twilio', async () => {
      parentAnswers()
      mockApi({
        twilio: false,
        // Still mapped to a subaccount - it is simply not what this page bills on.
        perMonth: { [THIS_MONTH]: { ...mapped, usage: pausedUsage, twilio_connected: true } },
      })

      render(<AdminPage />)
      await screen.findByText('Cool Breeze HVAC')

      expect(screen.getByText('Mapped')).toHaveAttribute('title', 'Retell AI mapped')
      expect(screen.queryByText(/Twilio/)).not.toBeInTheDocument()
    })

    it('says a client with no working Retell key so, without naming Twilio', async () => {
      parentAnswers()
      mockApi({
        twilio: false,
        perMonth: {
          [THIS_MONTH]: {
            ...mapped,
            usage: pausedUsage,
            retell_connected: false,
            twilio_connected: true,
          },
        },
      })

      render(<AdminPage />)
      await screen.findByText('Cool Breeze HVAC')

      expect(screen.getByText('Mapped')).toHaveAttribute('title', 'No working Retell key')
      // Said out loud too: this client records no calls at all.
      expect(screen.getByText('No key')).toBeInTheDocument()
    })

    it('brings the whole Twilio half back the moment the flag is on again', async () => {
      parentAnswers()
      mockApi({
        twilio: true,
        detail: { twilio_days: [twilioDay], usage: { ...mapped.usage!, twilio_stale: true } },
      })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)

      // Nothing was deleted: the tile, the rate field, the picker, the phone numbers and the
      // daily table are all exactly where they were.
      expect(tile('Call minutes').getByText('100.00')).toBeInTheDocument()
      expect(screen.getByLabelText('Call minute rate')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'ACchild' })).toBeInTheDocument()
      expect(screen.getByLabelText('Add a phone number')).toBeInTheDocument()
      expect(screen.getByText('Twilio calls by day')).toBeInTheDocument()
      expect(screen.getByText('Stale')).toBeInTheDocument()
      expect(screen.getByText(/Current through/)).toBeInTheDocument()
      // And the money counts both halves again: $55.00 less $18.00 to Retell and $4.20 to Twilio.
      expect(tile('Cost').getByText('$22.20')).toBeInTheDocument()
      expect(tile('Margin').getByText('$32.80')).toBeInTheDocument()
    })
  })

  // --- the card on file -------------------------------------------------------------------

  describe('card on file', () => {
    const requestButton = () => screen.queryByRole('button', { name: 'Request card' })

    it('shows the card a client can be charged with, and nothing to press', async () => {
      parentAnswers()
      mockApi()
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)

      expect(screen.getByText('Card on file')).toBeInTheDocument()
      expect(screen.getByText(/Visa •••• 4242/)).toBeInTheDocument()
      expect(requestButton()).not.toBeInTheDocument()
    })

    it('offers the button to a client with a customer but no card', async () => {
      parentAnswers()
      mockApi({ detail: { card: NO_CARD } })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)

      expect(screen.getByText('No card on file')).toBeInTheDocument()
      expect(requestButton()).toBeInTheDocument()
    })

    it('offers the button when no Stripe customer matched the email either', async () => {
      parentAnswers()
      mockApi({ detail: { card: { ...NO_CARD, status: 'no_customer' } } })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)

      expect(screen.getByText('No Stripe customer matches this email yet.')).toBeInTheDocument()
      expect(requestButton()).toBeInTheDocument()
    })

    it('has nothing to offer a client with no email in GoHighLevel', async () => {
      parentAnswers()
      mockApi({ detail: { card: { ...NO_CARD, status: 'no_billing_email' } } })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)

      expect(
        screen.getByText('No email on the sub-account owner in GoHighLevel.'),
      ).toBeInTheDocument()
      expect(requestButton()).not.toBeInTheDocument()
    })

    it('says it could not check rather than inventing a problem to chase', async () => {
      parentAnswers()
      mockApi({ detail: { card: { ...NO_CARD, status: 'unknown' } } })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)

      expect(screen.getByText('Could not check')).toBeInTheDocument()
      expect(screen.queryByText('No card on file')).not.toBeInTheDocument()
      expect(requestButton()).not.toBeInTheDocument()
    })

    it('asks for a card once and shows when it was asked for', async () => {
      parentAnswers()
      const fetchMock = mockApi({ detail: { card: NO_CARD } })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)
      await user.click(requestButton()!)

      await screen.findByText(/Card requested/)
      const asked = fetchMock.mock.calls.filter(([url]) =>
        String(url).includes('/card-request/'),
      )
      expect(asked).toHaveLength(1)
      expect(asked[0][1]).toMatchObject({ method: 'POST' })
    })

    it('shows a card attached between the page loading and the button being pressed', async () => {
      parentAnswers()
      mockApi({
        detail: { card: NO_CARD },
        cardRequest: { status: 'card_on_file', brand: 'visa', last4: '4242', requested_at: null },
      })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)
      await user.click(requestButton()!)

      expect(await screen.findByText('Card on file')).toBeInTheDocument()
      expect(requestButton()).not.toBeInTheDocument()
    })

    it('says so when GoHighLevel refused the request, and leaves the button there', async () => {
      parentAnswers()
      mockApi({ detail: { card: NO_CARD }, cardRequest: 502 })
      const user = userEvent.setup()

      render(<AdminPage />)
      await openClient(user)
      await user.click(requestButton()!)

      expect(await screen.findByRole('alert')).toHaveTextContent(/Could not ask GoHighLevel/)
      expect(requestButton()).toBeInTheDocument()
      expect(screen.queryByText(/Card requested/)).not.toBeInTheDocument()
    })

    it('never offers a sub-account the button on its own page', async () => {
      parentAnswers()
      mockApi({
        session: { ...agencySession, active_location: 'loc1', scope: 'location' },
        detail: { card: NO_CARD },
      })

      render(<AdminPage />)

      expect(await screen.findByText('No card on file')).toBeInTheDocument()
      expect(requestButton()).not.toBeInTheDocument()
    })
  })
})
