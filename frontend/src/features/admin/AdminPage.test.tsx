import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ClientDetail, ClientSummary, ClientsResponse, SsoSession } from '../../types/admin'
import AdminPage from './AdminPage'
import { currentMonth, shiftMonth } from './format'

const BLOB = 'U2FsdGVkX1-pretend-session'
const THIS_MONTH = currentMonth()
const LAST_MONTH = shiftMonth(THIS_MONTH, -1)

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
  twilio_connected: false,
  rates: null,
  usage: null,
}

const detail: ClientDetail = {
  month: THIS_MONTH,
  location_id: 'loc1',
  client_name: 'Cool Breeze HVAC',
  active: true,
  billing_mode: 'auto_charge',
  retell_agent_ids: ['agent_1'],
  phone_numbers: ['+15551234567'],
  twilio_subaccount_sid: 'ACchild',
  rates: { voice_rate_cents: 25, call_rate_cents: 5, start_date: `${THIS_MONTH}-01` },
  rate_history: [{ voice_rate_cents: 25, call_rate_cents: 5, start_date: `${THIS_MONTH}-01` }],
  usage: mapped.usage!,
  twilio_days: [],
  recent_calls: [],
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
 * `perMonth` overrides the client returned for a given month.
 */
function mockApi(options: { session?: SsoSession; perMonth?: Record<string, ClientSummary> } = {}) {
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

  it('shows money and minutes for a mapped client', async () => {
    parentAnswers()
    mockApi()

    render(<AdminPage />)
    await screen.findByText('Cool Breeze HVAC')

    expect(screen.getByText('200.00')).toBeInTheDocument()
    expect(screen.getByText('$55.00')).toBeInTheDocument()
    // Margin: $55.00 billed less $18.00 Retell and $4.20 Twilio.
    expect(screen.getByText('$32.80')).toBeInTheDocument()
  })

  it('shows an unmapped client as not set up rather than as zero usage', async () => {
    parentAnswers()
    mockApi()

    render(<AdminPage />)
    await screen.findByText('Polar Peak Heating')

    expect(screen.getByText('Not mapped')).toBeInTheDocument()
    expect(screen.getByText('Not set up for billing yet')).toBeInTheDocument()
    expect(screen.queryByText('0.00')).not.toBeInTheDocument()
  })

  it('opens a client when its name is clicked', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))

    expect(await screen.findByText('Mapping')).toBeInTheDocument()
    expect(screen.getByText('agent_1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '← All clients' })).toBeInTheDocument()
  })

  it('opens straight into the one client when GHL says this is a sub-account', async () => {
    parentAnswers()
    mockApi({ session: { ...agencySession, active_location: 'loc1', scope: 'location' } })

    render(<AdminPage />)

    expect(await screen.findByText('Mapping')).toBeInTheDocument()
    // No way back to a list that this session is not allowed to see.
    expect(screen.queryByRole('button', { name: '← All clients' })).not.toBeInTheDocument()
  })

  it('shows a skeleton while the clients are loading', async () => {
    parentAnswers()
    mockApi()

    render(<AdminPage />)

    expect(screen.getByLabelText('Loading clients')).toBeInTheDocument()
    await screen.findByText('Cool Breeze HVAC')
    expect(screen.queryByLabelText('Loading clients')).not.toBeInTheDocument()
  })

  // --- the period filter ---------------------------------------------------------------

  it('defaults the period to the current billing month', async () => {
    parentAnswers()
    const fetchMock = mockApi()

    render(<AdminPage />)
    await screen.findByText('Cool Breeze HVAC')

    expect(monthSelect().value).toBe(THIS_MONTH)
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).startsWith(`/api/rebilling/admin/clients/?month=${THIS_MONTH}`),
      ),
    ).toBe(true)
  })

  it('refetches the clients for the period that was chosen', async () => {
    parentAnswers()
    const quietMonth: ClientSummary = {
      ...mapped,
      usage: { ...mapped.usage!, ai_minutes: '12.00', estimated_bill_cents: 300 },
    }
    const fetchMock = mockApi({ perMonth: { [LAST_MONTH]: quietMonth } })
    const user = userEvent.setup()

    render(<AdminPage />)
    await screen.findByText('200.00')

    await user.selectOptions(monthSelect(), LAST_MONTH)

    expect(await screen.findByText('12.00')).toBeInTheDocument()
    expect(screen.getByText('$3.00')).toBeInTheDocument()
    expect(screen.queryByText('200.00')).not.toBeInTheDocument()
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).startsWith(`/api/rebilling/admin/clients/?month=${LAST_MONTH}`),
      ),
    ).toBe(true)
  })

  it('steps a month at a time with the arrows', async () => {
    parentAnswers()
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await screen.findByText('Cool Breeze HVAC')

    await user.click(screen.getByLabelText('Previous month'))
    await waitFor(() => expect(monthSelect().value).toBe(LAST_MONTH))

    await user.click(screen.getByLabelText('Next month'))
    await waitFor(() => expect(monthSelect().value).toBe(THIS_MONTH))
  })

  it('will not step past the current month, because nothing is billed in the future', async () => {
    parentAnswers()
    mockApi()

    render(<AdminPage />)
    await screen.findByText('Cool Breeze HVAC')

    expect(screen.getByLabelText('Next month')).toBeDisabled()
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
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))
    await screen.findByText('Mapping')

    await user.selectOptions(monthSelect(), LAST_MONTH)

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).startsWith(`/api/rebilling/admin/clients/loc1/?month=${LAST_MONTH}`),
        ),
      ).toBe(true),
    )
    const label = await screen.findByText('AI minutes')
    expect(within(label.closest('.tile') as HTMLElement).getByText('7.00')).toBeInTheDocument()
  })

  it('never shows one period under another period heading', async () => {
    parentAnswers()
    const fetchMock = mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await screen.findByText('200.00')

    // The request for last month never resolves, so the page must keep waiting rather than
    // leave this month's numbers on screen under the new label.
    fetchMock.mockImplementation(() => new Promise(() => {}))
    await user.selectOptions(monthSelect(), LAST_MONTH)

    expect(await screen.findByLabelText('Loading clients')).toBeInTheDocument()
    expect(screen.queryByText('200.00')).not.toBeInTheDocument()
  })

  // --- standalone preview --------------------------------------------------------------

  it('falls back to a marked sample preview when there is no GoHighLevel to ask', async () => {
    // Framed, but nothing answers: the handshake times out and the preview takes over.
    parentIgnores()
    const fetchMock = mockApi()

    render(<AdminPage />)
    await vi.advanceTimersByTimeAsync(10_000)

    expect(await screen.findByText('Standalone preview.', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('Cool Breeze HVAC')).toBeInTheDocument()
    // The preview must never reach the authenticated API.
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('previews immediately when the page is not framed at all', async () => {
    // window.parent === window when opened directly in a browser, so there is nothing to wait
    // for and no timeout to sit through.
    const fetchMock = mockApi()

    render(<AdminPage />)

    expect(await screen.findByText('Standalone preview.', { exact: false })).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('can open a client in the preview', async () => {
    mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    await user.click(await screen.findByRole('button', { name: 'Polar Peak Heating & Air' }))

    expect(await screen.findByText('Mapping')).toBeInTheDocument()
    expect(screen.getByText('agent_polar_peak')).toBeInTheDocument()
    expect(screen.getByText('Rates')).toBeInTheDocument()
  })

  it('filters the preview by period too, without calling the API', async () => {
    const fetchMock = mockApi()
    const user = userEvent.setup()

    render(<AdminPage />)
    expect(await screen.findByText('198.40')).toBeInTheDocument()

    await user.selectOptions(monthSelect(), LAST_MONTH)

    await waitFor(() => expect(monthSelect().value).toBe(LAST_MONTH))
    // Different period, different figures — and still nothing fetched.
    expect(screen.queryByText('198.40')).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
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
