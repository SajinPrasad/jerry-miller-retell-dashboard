import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ClientDetail, ClientSummary, ClientsResponse, SsoSession } from '../../types/admin'
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
  active: true,
  billing_mode: 'auto_charge',
  retell_agent_ids: ['agent_1'],
  phone_numbers: ['+15551234567'],
  twilio_subaccount_sid: 'ACchild',
  rates: { voice_rate_cents: 25, call_rate_cents: 5, start_date: `${THIS_MONTH}-01` },
  rate_history: [{ voice_rate_cents: 25, call_rate_cents: 5, start_date: `${THIS_MONTH}-01` }],
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
    },
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
  } = {},
) {
  const { session = agencySession, perMonth = {} } = options

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

    if (url.startsWith('/api/rebilling/admin/clients/loc1/')) {
      return json({ ...detail, month, usage: (perMonth[month] ?? mapped).usage })
    }

    if (url.startsWith('/api/rebilling/admin/clients/sync/')) {
      const body: ClientsResponse = {
        scope: session.scope,
        month,
        clients: [...(options.synced ?? [perMonth[month] ?? mapped, unmapped])],
      }
      return json(body)
    }

    if (url.startsWith('/api/rebilling/admin/clients/')) {
      const body: ClientsResponse = {
        scope: session.scope,
        month,
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
async function openClient(user: ReturnType<typeof userEvent.setup>, name = 'Cool Breeze HVAC') {
  await user.click(await screen.findByRole('button', { name }))
  await screen.findByText('Mapping')
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

    await screen.findByText('Mapping')
    expect(screen.queryByRole('button', { name: /Refresh/ })).not.toBeInTheDocument()
  })

  // --- the client's own page ------------------------------------------------------------

  it('opens a client when its name is clicked', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    expect(screen.getByText('agent_1')).toBeInTheDocument()
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

  it('opens straight into the one client when GHL says this is a sub-account', async () => {
    parentAnswers()
    mockApi({ session: { ...agencySession, active_location: 'loc1', scope: 'location' } })

    render(<AdminPage />)

    expect(await screen.findByText('Mapping')).toBeInTheDocument()
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

  it('puts the client back on its own rate', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await openClient(user)

    const callRate = screen.getByLabelText('Call minute rate')
    await user.clear(callRate)
    await user.type(callRate, '0.20')
    await waitFor(() => expect(tile('Estimated').getByText('$70.00')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Reset' }))

    expect(tile('Estimated').getByText('$55.00')).toBeInTheDocument()
    expect(screen.getByLabelText('Call minute rate')).toHaveValue('0.05')
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
        })
      }
      if (url.startsWith('/api/rebilling/admin/clients/')) {
        return json({ scope: 'location', month: THIS_MONTH, clients: [] })
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
})
