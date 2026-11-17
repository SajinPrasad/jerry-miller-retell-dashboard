import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Backfill, HcpState, HcpStatus } from '../../types/hcp'
import AdminPreviewPage from './AdminPreviewPage'
import HousecallProTab from './HousecallProTab'

const LOC = 'loc1'
const BASE = `/api/hcp/accounts/${LOC}`

const mappings = [
  {
    id: 11,
    hcp_employee_id: 'emp-1',
    hcp_employee_name: 'Ana Ruiz',
    ghl_user_id: 'user-1',
    ghl_user_name: 'Ana R',
  },
  {
    id: 12,
    hcp_employee_id: 'emp-2',
    hcp_employee_name: 'Ben Cole',
    ghl_user_id: 'user-2',
    ghl_user_name: 'Sam Fox',
  },
]

const idle: Backfill = { status: 'idle', stage: '', progress: {} }

function status(state: HcpState, overrides: Partial<HcpStatus> = {}): HcpStatus {
  const connected = state !== 'not_connected'
  return {
    location_id: LOC,
    state,
    is_active: state === 'active',
    backfill: connected ? idle : null,
    account: connected
      ? {
          hcp_company_id: 'hcp-1',
          company_name: 'Cool Breeze HVAC',
          company_phone: '',
          company_email: '',
          timezone: '',
          connected_at: '2026-11-17T10:00:00Z',
        }
      : null,
    employee_mappings: ['needs_calendar_settings', 'ready_to_activate', 'backfilling', 'active'].includes(
      state,
    )
      ? mappings
      : [],
    calendar_settings: ['ready_to_activate', 'backfilling', 'active'].includes(state)
      ? {
          estimate_calendar_id: 'cal-est',
          estimate_calendar_name: 'Estimates',
          job_calendar_id: 'cal-job',
          job_calendar_name: 'Jobs',
          default_user: mappings[0],
          estimate_default_user: null,
          job_default_user: null,
        }
      : null,
    ...overrides,
  }
}

const directory = {
  hcp_employees: [
    { id: 'emp-1', name: 'Ana Ruiz', email: 'ana@example.test' },
    { id: 'emp-2', name: 'Ben Cole', email: 'ben@example.test' },
  ],
  ghl_users: [
    { id: 'user-1', name: 'Ana R', email: 'ana@example.test' },
    { id: 'user-2', name: 'Sam Fox', email: 'sam@example.test' },
  ],
  mappings: [],
}

const calendars = {
  calendars: [
    { id: 'cal-est', name: 'Estimates' },
    { id: 'cal-job', name: 'Jobs' },
  ],
}

type Reply = { status?: number; body?: unknown }

/**
 * Answers `fetch` by method and path. A route given an array answers with each entry in turn and
 * then keeps repeating the last, which is how a status that changes between polls is staged.
 * Anything unrouted fails the test rather than quietly returning nothing.
 */
function routeFetch(routes: Record<string, Reply | Reply[]>) {
  const calls: { key: string; body: unknown }[] = []
  const queues = new Map(
    Object.entries(routes).map(([key, value]) => [key, Array.isArray(value) ? [...value] : [value]]),
  )
  vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const key = `${init?.method ?? 'GET'} ${String(input)}`
    const queue = queues.get(key)
    if (!queue) throw new Error(`unrouted request: ${key}`)
    calls.push({ key, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    const reply = queue.length > 1 ? queue.shift()! : queue[0]
    return Promise.resolve(
      new Response(JSON.stringify(reply.body ?? {}), { status: reply.status ?? 200 }),
    )
  })
  return calls
}

function renderTab(props: { preview?: boolean } = {}) {
  return render(<HousecallProTab locationId={LOC} preview={props.preview ?? false} pollMs={10} />)
}

describe('HousecallProTab', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  describe('step 1: connect', () => {
    it('sends the key and moves on to employee mapping', async () => {
      const user = userEvent.setup()
      const calls = routeFetch({
        [`GET ${BASE}/status/`]: { body: status('not_connected') },
        'POST /api/hcp/accounts/': { status: 201, body: status('needs_employee_mapping') },
        [`GET ${BASE}/employees/`]: { body: directory },
      })

      renderTab()
      await user.type(await screen.findByLabelText('Housecall Pro API key'), 'hcp-secret')
      await user.click(screen.getByRole('button', { name: 'Connect' }))

      expect(await screen.findByText('Step 2 of 4 — Map employees')).toBeInTheDocument()
      expect(calls.find((call) => call.key === 'POST /api/hcp/accounts/')?.body).toEqual({
        location_id: LOC,
        api_key: 'hcp-secret',
      })
    })

    it('keeps Connect disabled until a key is typed', async () => {
      routeFetch({ [`GET ${BASE}/status/`]: { body: status('not_connected') } })

      renderTab()

      expect(await screen.findByRole('button', { name: 'Connect' })).toBeDisabled()
    })

    it.each([
      [400, { code: 'hcp_invalid_api_key', detail: 'rejected' }, 'rejected that API key'],
      [429, { code: 'hcp_rate_limited', detail: 'slow' }, 'rate limiting this account'],
      [502, { code: 'hcp_unavailable', detail: 'down' }, 'Housecall Pro could not be reached'],
    ])('explains a %s from Housecall Pro', async (httpStatus, body, expected) => {
      const user = userEvent.setup()
      routeFetch({
        [`GET ${BASE}/status/`]: { body: status('not_connected') },
        'POST /api/hcp/accounts/': { status: httpStatus, body },
      })

      renderTab()
      await user.type(await screen.findByLabelText('Housecall Pro API key'), 'k')
      await user.click(screen.getByRole('button', { name: 'Connect' }))

      expect(await screen.findByRole('alert')).toHaveTextContent(expected)
      // Still on step 1, with the key to correct.
      expect(screen.getByLabelText('Housecall Pro API key')).toBeInTheDocument()
    })
  })

  describe('step 2: employees', () => {
    it('maps each employee to a user, and offers each user only once', async () => {
      const user = userEvent.setup()
      const calls = routeFetch({
        [`GET ${BASE}/status/`]: { body: status('needs_employee_mapping') },
        [`GET ${BASE}/employees/`]: { body: directory },
        [`PUT ${BASE}/employee-mappings/`]: { body: status('needs_calendar_settings') },
        [`GET ${BASE}/calendars/`]: { body: calendars },
      })

      renderTab()
      const ana = await screen.findByLabelText('GoHighLevel user for Ana Ruiz')
      const ben = screen.getByLabelText('GoHighLevel user for Ben Cole')
      expect(screen.getByRole('button', { name: 'Save mappings' })).toBeDisabled()

      await user.selectOptions(ana, 'user-1')

      // Ana's user is taken, so Ben cannot also be given it.
      expect(within(ben).getByRole('option', { name: 'Ana R' })).toBeDisabled()
      expect(within(ana).getByRole('option', { name: 'Ana R' })).toBeEnabled()

      await user.selectOptions(ben, 'user-2')
      await user.click(screen.getByRole('button', { name: 'Save mappings' }))

      expect(await screen.findByText('Step 3 of 4 — Calendars')).toBeInTheDocument()
      expect(calls.find((call) => call.key === `PUT ${BASE}/employee-mappings/`)?.body).toEqual({
        mappings: [
          {
            hcp_employee_id: 'emp-1',
            hcp_employee_name: 'Ana Ruiz',
            ghl_user_id: 'user-1',
            ghl_user_name: 'Ana R',
          },
          {
            hcp_employee_id: 'emp-2',
            hcp_employee_name: 'Ben Cole',
            ghl_user_id: 'user-2',
            ghl_user_name: 'Sam Fox',
          },
        ],
      })
    })

    it('starts from what is already mapped', async () => {
      routeFetch({
        [`GET ${BASE}/status/`]: { body: status('needs_employee_mapping') },
        [`GET ${BASE}/employees/`]: { body: { ...directory, mappings: [mappings[0]] } },
      })

      renderTab()

      expect(await screen.findByLabelText('GoHighLevel user for Ana Ruiz')).toHaveValue('user-1')
      expect(screen.getByLabelText('GoHighLevel user for Ben Cole')).toHaveValue('')
    })
  })

  describe('step 3: calendars', () => {
    function calendarRoutes(save: Reply) {
      return {
        [`GET ${BASE}/status/`]: { body: status('needs_calendar_settings') },
        [`GET ${BASE}/calendars/`]: { body: calendars },
        [`PUT ${BASE}/calendar-settings/`]: save,
      }
    }

    async function fillRequired(user: ReturnType<typeof userEvent.setup>) {
      await user.selectOptions(await screen.findByLabelText('Estimate calendar'), 'cal-est')
      await user.selectOptions(screen.getByLabelText('Job calendar'), 'cal-job')
      await user.selectOptions(screen.getByLabelText('Default user'), '11')
    }

    it('saves the calendars and the default user, with optional overrides left empty', async () => {
      const user = userEvent.setup()
      const calls = routeFetch(calendarRoutes({ body: status('ready_to_activate') }))

      renderTab()
      expect(await screen.findByRole('button', { name: 'Save calendars' })).toBeDisabled()
      await fillRequired(user)
      await user.click(screen.getByRole('button', { name: 'Save calendars' }))

      expect(await screen.findByText('Step 4 of 4 — Activate')).toBeInTheDocument()
      expect(calls.find((call) => call.key === `PUT ${BASE}/calendar-settings/`)?.body).toEqual({
        estimate_calendar_id: 'cal-est',
        job_calendar_id: 'cal-job',
        default_user: 11,
        estimate_default_user: null,
        job_default_user: null,
      })
    })

    it('sends a per-calendar override when one is chosen', async () => {
      const user = userEvent.setup()
      const calls = routeFetch(calendarRoutes({ body: status('ready_to_activate') }))

      renderTab()
      await fillRequired(user)
      await user.selectOptions(screen.getByLabelText('Job calendar user (optional)'), '12')
      await user.click(screen.getByRole('button', { name: 'Save calendars' }))

      await screen.findByText('Step 4 of 4 — Activate')
      const sent = calls.find((call) => call.key === `PUT ${BASE}/calendar-settings/`)
      expect(sent?.body).toMatchObject({ job_default_user: 12, estimate_default_user: null })
    })

    it('does not offer the estimate calendar as the job calendar', async () => {
      const user = userEvent.setup()
      routeFetch(calendarRoutes({ body: status('ready_to_activate') }))

      renderTab()
      await user.selectOptions(await screen.findByLabelText('Estimate calendar'), 'cal-est')

      expect(
        within(screen.getByLabelText('Job calendar')).getByRole('option', { name: 'Estimates' }),
      ).toBeDisabled()
    })

    it('names who is missing from which calendar team when the backend refuses', async () => {
      const user = userEvent.setup()
      routeFetch(
        calendarRoutes({
          status: 400,
          body: {
            detail: 'Add Sam Fox to the team on Jobs in GoHighLevel, then save again.',
            missing_team_members: {
              job_calendar_id: [{ ghl_user_id: 'user-2', ghl_user_name: 'Sam Fox' }],
            },
          },
        }),
      )

      renderTab()
      await fillRequired(user)
      await user.click(screen.getByRole('button', { name: 'Save calendars' }))

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('Add Sam Fox to the team on Jobs.')
      // The step stays open to be saved again once the team is fixed in GoHighLevel.
      expect(screen.getByRole('button', { name: 'Save calendars' })).toBeEnabled()
    })
  })

  describe('step 4: activate and backfill', () => {
    it('starts the backfill and shows stage progress, then the live state', async () => {
      const user = userEvent.setup()
      const running: Backfill = {
        status: 'running',
        stage: 'leads',
        progress: { customers: { synced: 40, skipped: 2 }, leads: { synced: 5 } },
      }
      const done: Backfill = {
        status: 'completed',
        stage: 'jobs',
        progress: {
          customers: { synced: 40 },
          leads: { synced: 9 },
          estimates: { synced: 3 },
          jobs: { synced: 7 },
        },
      }
      const calls = routeFetch({
        [`GET ${BASE}/status/`]: [
          { body: status('ready_to_activate') },
          { body: status('backfilling', { backfill: running }) },
          { body: status('active', { backfill: done }) },
        ],
        [`POST ${BASE}/activate/`]: {
          status: 202,
          body: status('backfilling', { backfill: { ...running, progress: {} } }),
        },
      })

      renderTab()
      await user.click(await screen.findByRole('button', { name: 'Activate' }))

      expect(calls.some((call) => call.key === `POST ${BASE}/activate/`)).toBe(true)
      const list = await screen.findByRole('list', { name: 'Backfill progress' })
      expect(within(list).getByText('Customers').closest('li')).toHaveTextContent('Done')

      // Polling picks up the counts, then the finished account.
      await waitFor(() => expect(screen.getByText('40 synced, 2 skipped')).toBeInTheDocument())
      expect(within(list).getByText('Leads').closest('li')).toHaveTextContent('Running')
      expect(within(list).getByText('Jobs').closest('li')).toHaveTextContent('Waiting')

      expect(await screen.findByText('Active')).toBeInTheDocument()
      expect(screen.getByText('Cool Breeze HVAC')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Activate' })).not.toBeInTheDocument()
    })

    it('stops polling once the backfill is no longer running', async () => {
      const fetchCalls = routeFetch({
        [`GET ${BASE}/status/`]: [
          { body: status('backfilling', { backfill: { ...idle, status: 'running', stage: 'jobs' } }) },
          { body: status('active', { backfill: { ...idle, status: 'completed', stage: 'jobs' } }) },
        ],
      })

      renderTab()
      await screen.findByText('Active')
      const seen = fetchCalls.length
      await new Promise((resolve) => setTimeout(resolve, 60))

      expect(fetchCalls.length).toBe(seen)
    })

    it('offers a retry, with where it stopped, after a failed backfill', async () => {
      const user = userEvent.setup()
      const failed: Backfill = {
        status: 'failed',
        stage: 'estimates',
        progress: { customers: { synced: 40 }, leads: { synced: 9 }, estimates: { synced: 1, failed: 2 } },
      }
      const calls = routeFetch({
        [`GET ${BASE}/status/`]: [
          { body: status('ready_to_activate', { backfill: failed }) },
          { body: status('backfilling', { backfill: { status: 'running', stage: '', progress: {} } }) },
        ],
        [`POST ${BASE}/activate/`]: {
          status: 202,
          body: status('backfilling', { backfill: { status: 'running', stage: '', progress: {} } }),
        },
      })

      renderTab()

      expect(await screen.findByRole('alert')).toHaveTextContent('stopped while pulling estimates')
      const list = screen.getByRole('list', { name: 'Backfill progress' })
      expect(within(list).getByText('Estimates').closest('li')).toHaveTextContent('Stopped')
      expect(within(list).getByText('1 synced, 2 failed')).toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: 'Retry activation' }))

      await waitFor(() =>
        expect(calls.some((call) => call.key === `POST ${BASE}/activate/`)).toBe(true),
      )
      await waitFor(() =>
        expect(screen.queryByRole('button', { name: 'Retry activation' })).not.toBeInTheDocument(),
      )
      expect(screen.getByRole('list', { name: 'Backfill progress' })).toBeInTheDocument()
    })

    it('shows the running backfill when another admin started it first (409)', async () => {
      const user = userEvent.setup()
      routeFetch({
        [`GET ${BASE}/status/`]: [
          { body: status('ready_to_activate') },
          { body: status('backfilling', { backfill: { status: 'running', stage: 'customers', progress: {} } }) },
        ],
        [`POST ${BASE}/activate/`]: {
          status: 409,
          body: { detail: 'The backfill is already running.', code: 'backfill_running' },
        },
      })

      renderTab()
      await user.click(await screen.findByRole('button', { name: 'Activate' }))

      expect(await screen.findByRole('list', { name: 'Backfill progress' })).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
  })

  describe('editing an earlier step', () => {
    const employeesRoutes = (after: HcpStatus) => ({
      [`GET ${BASE}/employees/`]: { body: { ...directory, mappings } },
      [`PUT ${BASE}/employee-mappings/`]: { body: after },
    })

    it('re-opens the employees from the ready screen, prefilled, and returns when saved', async () => {
      const user = userEvent.setup()
      const calls = routeFetch({
        [`GET ${BASE}/status/`]: { body: status('ready_to_activate') },
        ...employeesRoutes(status('ready_to_activate')),
      })

      renderTab()
      await user.click(await screen.findByRole('button', { name: 'Edit employees' }))

      expect(await screen.findByText('Edit employees', { selector: 'p' })).toBeInTheDocument()
      const ben = await screen.findByLabelText('GoHighLevel user for Ben Cole')
      expect(ben).toHaveValue('user-2')
      await user.selectOptions(ben, '')
      await user.click(screen.getByRole('button', { name: 'Save mappings' }))

      expect(await screen.findByRole('button', { name: 'Activate' })).toBeInTheDocument()
      const sent = calls.find((call) => call.key === `PUT ${BASE}/employee-mappings/`)
      expect(sent?.body).toMatchObject({ mappings: [{ hcp_employee_id: 'emp-1' }] })
      expect(Object.keys(sent?.body ?? {})).toEqual(['mappings'])
    })

    it('leaves an edit without saving on Cancel', async () => {
      const user = userEvent.setup()
      const calls = routeFetch({
        [`GET ${BASE}/status/`]: { body: status('active', { backfill: { ...idle, status: 'completed', stage: 'jobs' } }) },
        ...employeesRoutes(status('active')),
      })

      renderTab()
      await user.click(await screen.findByRole('button', { name: 'Edit employees' }))
      await screen.findByLabelText('GoHighLevel user for Ana Ruiz')
      await user.click(screen.getByRole('button', { name: 'Cancel' }))

      expect(await screen.findByText('Active')).toBeInTheDocument()
      expect(calls.some((call) => call.key.startsWith('PUT'))).toBe(false)
    })

    it('re-opens the calendars of a live account, with its settings filled in', async () => {
      const user = userEvent.setup()
      const calls = routeFetch({
        [`GET ${BASE}/status/`]: { body: status('active', { backfill: { ...idle, status: 'completed', stage: 'jobs' } }) },
        [`GET ${BASE}/calendars/`]: { body: calendars },
        [`PUT ${BASE}/calendar-settings/`]: { body: status('active') },
      })

      renderTab()
      await user.click(await screen.findByRole('button', { name: 'Edit calendars' }))

      expect(await screen.findByLabelText('Default user')).toHaveValue('11')
      await waitFor(() => expect(screen.getByLabelText('Job calendar')).toHaveValue('cal-job'))
      await user.selectOptions(screen.getByLabelText('Job calendar user (optional)'), '12')
      await user.click(screen.getByRole('button', { name: 'Save calendars' }))

      expect(await screen.findByText('Active')).toBeInTheDocument()
      const sent = calls.find((call) => call.key === `PUT ${BASE}/calendar-settings/`)
      expect(sent?.body).toMatchObject({ job_default_user: 12, default_user: 11 })
    })

    it('can go back to the employees from the calendars step', async () => {
      const user = userEvent.setup()
      routeFetch({
        [`GET ${BASE}/status/`]: { body: status('needs_calendar_settings') },
        [`GET ${BASE}/calendars/`]: { body: calendars },
        ...employeesRoutes(status('needs_calendar_settings')),
      })

      renderTab()
      await user.click(await screen.findByRole('button', { name: '← Edit employees' }))
      await screen.findByLabelText('GoHighLevel user for Ana Ruiz')
      await user.click(screen.getByRole('button', { name: 'Save mappings' }))

      expect(await screen.findByText('Step 3 of 4 — Calendars')).toBeInTheDocument()
    })

    it('offers no way back from the first two steps', async () => {
      routeFetch({ [`GET ${BASE}/status/`]: { body: status('needs_employee_mapping') }, [`GET ${BASE}/employees/`]: { body: directory } })

      renderTab()
      await screen.findByLabelText('GoHighLevel user for Ana Ruiz')

      expect(screen.queryByRole('button', { name: /Edit/ })).not.toBeInTheDocument()
    })

    it('is disabled while the backfill runs', async () => {
      routeFetch({
        [`GET ${BASE}/status/`]: {
          body: status('backfilling', { backfill: { status: 'running', stage: 'leads', progress: {} } }),
        },
      })

      renderTab()

      expect(await screen.findByRole('button', { name: 'Edit employees' })).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Edit calendars' })).toBeDisabled()
    })
  })

  it('says so when the status cannot be loaded, and lets it be retried', async () => {
    const user = userEvent.setup()
    routeFetch({
      [`GET ${BASE}/status/`]: [{ status: 502 }, { body: status('not_connected') }],
    })

    renderTab()
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load')
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByLabelText('Housecall Pro API key')).toBeInTheDocument()
  })

  it('makes no request at all in preview', () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')

    renderTab({ preview: true })

    expect(screen.getByText(/isn.t available in the standalone preview/)).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('Housecall Pro tab on the client page', () => {
  it('is on the unmapped-client screen too, without touching the API in preview', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Summit Air Solutions' }))
    expect(await screen.findByText('Not set up for billing yet.', { exact: false })).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Housecall Pro' }))
    expect(screen.getByText(/isn.t available in the standalone preview/)).toBeInTheDocument()
    expect(screen.queryByText('Not set up for billing yet.', { exact: false })).not.toBeInTheDocument()

    // And back to the mapping it opened on.
    await user.click(screen.getByRole('tab', { name: 'Agent mapping' }))
    expect(screen.getByText('Not set up for billing yet.', { exact: false })).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('is a third tab, and in the standalone preview it never calls the API', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Polar Peak Heating & Air' }))
    await user.click(await screen.findByRole('tab', { name: 'Housecall Pro' }))

    expect(screen.getByText(/isn.t available in the standalone preview/)).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
