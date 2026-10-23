import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ClientDetail, ClientSummary, ClientsResponse, SsoSession } from '../../types/admin'
import AdminPage from './AdminPage'

const BLOB = 'U2FsdGVkX1-pretend-session'

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
  rates: { voice_rate_cents: 25, call_rate_cents: 5, start_date: '2026-03-01' },
  usage: {
    ai_minutes: '200.00',
    ai_cost_cents: 1800,
    call_minutes: '100.00',
    call_cost_cents: 420,
    has_twilio_usage: true,
    estimated_bill_cents: 5500,
    unbillable_calls: 0,
    twilio_as_of: '2026-03-31T03:30:00Z',
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

const clientsResponse: ClientsResponse = {
  scope: 'agency',
  month: '2026-03',
  clients: [mapped, unmapped],
}

const detail: ClientDetail = {
  month: '2026-03',
  location_id: 'loc1',
  client_name: 'Cool Breeze HVAC',
  active: true,
  billing_mode: 'auto_charge',
  retell_agent_ids: ['agent_1'],
  phone_numbers: ['+15551234567'],
  twilio_subaccount_sid: 'ACchild',
  rates: { voice_rate_cents: 25, call_rate_cents: 5, start_date: '2026-03-01' },
  rate_history: [{ voice_rate_cents: 25, call_rate_cents: 5, start_date: '2026-03-01' }],
  usage: mapped.usage!,
  twilio_days: [],
  recent_calls: [],
}

/** Answer the page's handshake the way the GHL parent frame would. */
function parentAnswers(payload: unknown = BLOB) {
  vi.spyOn(window.parent, 'postMessage').mockImplementation((message: unknown) => {
    if ((message as { message?: string })?.message !== 'REQUEST_USER_DATA') return
    window.dispatchEvent(
      new MessageEvent('message', { data: { message: 'REQUEST_USER_DATA_RESPONSE', payload } }),
    )
  })
}

function mockApi(overrides: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    '/api/ghl/sso/session/': agencySession,
    '/api/rebilling/admin/clients/': clientsResponse,
    '/api/rebilling/admin/clients/loc1/': detail,
    ...overrides,
  }
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = String(input)
    // Longest first, so '/clients/loc1/' wins over the '/clients/' prefix.
    const match = Object.keys(routes)
      .sort((a, b) => b.length - a.length)
      .find((route) => url.startsWith(route))
    if (!match) return Promise.resolve(new Response('', { status: 404 }))
    return Promise.resolve(
      new Response(JSON.stringify(routes[match]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
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
    // The month and the viewer share one line, so match on the line.
    expect(screen.getByText(/March 2026/)).toBeInTheDocument()
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
    mockApi({
      '/api/ghl/sso/session/': { ...agencySession, active_location: 'loc1', scope: 'location' },
      '/api/rebilling/admin/clients/': { ...clientsResponse, scope: 'location', clients: [mapped] },
    })

    render(<AdminPage />)

    expect(await screen.findByText('Mapping')).toBeInTheDocument()
    // No way back to a list that this session is not allowed to see.
    expect(screen.queryByRole('button', { name: '← All clients' })).not.toBeInTheDocument()
  })

  it('explains itself when it is not running inside GoHighLevel', async () => {
    // No parent listener, so the handshake goes unanswered and the timeout fires.
    vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    mockApi()

    render(<AdminPage />)
    await vi.advanceTimersByTimeAsync(10_000)

    expect(
      await screen.findByText('Open this page from inside GoHighLevel.', { exact: false }),
    ).toBeInTheDocument()
  })

  it('reports a failed load rather than rendering an empty page', async () => {
    parentAnswers()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 500 }))

    render(<AdminPage />)

    await waitFor(() =>
      expect(screen.getByText('Could not load billing data.', { exact: false })).toBeInTheDocument(),
    )
  })
})
