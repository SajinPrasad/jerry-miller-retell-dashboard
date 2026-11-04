import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdminPreviewPage from './AdminPreviewPage'
import {
  resetPreviewCardRequests,
  resetPreviewKeys,
  resetPreviewMappings,
  resetPreviewRates,
} from './demoData'
import { currentMonth, shiftMonth } from './format'

const THIS_MONTH = currentMonth()
const LAST_MONTH = shiftMonth(THIS_MONTH, -1)

function monthSelect() {
  return screen.getByLabelText('Billing month') as HTMLSelectElement
}

/** The tile with this label, which is where the detail page's headline figures live. */
function tile(label: string) {
  const heading = screen.getByText(label, { selector: '.tile-label' })
  return within(heading.closest('.tile') as HTMLElement)
}

describe('AdminPreviewPage', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    // Edits made in one test must not bleed into the next.
    resetPreviewMappings()
    resetPreviewRates()
    resetPreviewCardRequests()
    resetPreviewKeys()
  })

  it('renders sample clients behind a clearly marked banner, with no network calls', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')

    render(<AdminPreviewPage />)

    expect(await screen.findByText('Standalone preview.', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('Cool Breeze HVAC')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('opens a sample client', async () => {
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Polar Peak Heating & Air' }))

    expect(await screen.findByText('Rates')).toBeInTheDocument()

    // The agent's own name, not its raw Retell id.
    await user.click(screen.getByRole('tab', { name: 'Retell workspace' }))
    expect(screen.getByText('Polar Peak Heating & Air — main line')).toBeInTheDocument()
    expect(screen.queryByText('agent_polar_peak')).not.toBeInTheDocument()
  })

  it('filters the preview by period, without calling the API', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))
    expect(tile('AI minutes').getByText('198.40')).toBeInTheDocument()

    await user.selectOptions(monthSelect(), LAST_MONTH)

    expect(monthSelect().value).toBe(LAST_MONTH)
    // Different period, different figures — and still nothing fetched.
    expect(screen.queryByText('198.40')).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('opens the unmapped sample client to set it up, with no network call', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Summit Air Solutions' }))

    expect(await screen.findByText('Not set up for billing yet.', { exact: false })).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('leaves Twilio out of the preview entirely, the way the paused backend does', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Polar Peak Heating & Air' }))
    await screen.findByRole('tab', { name: 'Retell workspace' })

    // The sample data still carries this client's SID, its phone numbers and its daily Twilio
    // rows - the preview simply does not show them while rebilling is paused.
    expect(
      screen.queryByRole('button', { name: 'ACdemopolarpeak00000000000000002' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Add a phone number')).not.toBeInTheDocument()
    expect(screen.queryByText('Twilio calls by day')).not.toBeInTheDocument()
    expect(screen.queryByText('Call minutes', { selector: '.tile-label' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Call minute rate')).not.toBeInTheDocument()
    // Still the one thing an admin sets up here.
    await user.click(screen.getByRole('tab', { name: 'Retell workspace' }))
    expect(screen.getByLabelText('Retell workspace API key')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('says why a sample client could not be matched in Stripe', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Branwynn Destinations' }))

    // The owner is known; Stripe has nobody at that address. That message is the whole of what
    // an admin can act on, and it names the address to fix.
    expect(await screen.findByText('Imogen Vale')).toBeInTheDocument()
    expect(
      screen.getByText(/No Stripe customer found for billing email: imogen@branwynn.test/),
    ).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('asks a sample client for a card without calling the API', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Harbor Heat & Cool' }))
    await user.click(await screen.findByRole('button', { name: 'Request card' }))

    expect(await screen.findByText(/Card requested/)).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('shows the card a sample client is charged with', async () => {
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))

    expect(await screen.findByText('Card on file')).toBeInTheDocument()
    expect(screen.getByText(/Visa •••• 4242/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Request card' })).not.toBeInTheDocument()
  })

  it('shows a sample bill being retried as In Retry on a finished month', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Harbor Heat & Cool' }))
    await user.selectOptions(monthSelect(), LAST_MONTH)

    expect(await screen.findByText('In Retry', { selector: '.tile-label' })).toHaveClass('pill', 'warn')
    expect(screen.getByText('Retry 2 of 3')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('saves a sample rate in the preview, and it sticks on reopen', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))

    const aiRate = screen.getByLabelText('AI minute rate')
    await user.clear(aiRate)
    await user.type(aiRate, '0.50')
    await user.click(screen.getByRole('button', { name: 'Save rates' }))

    await user.click(screen.getByRole('button', { name: '← All clients' }))
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))

    // Held in the sample data, like a preview mapping edit - and the estimate follows it:
    // 198.40 AI minutes at 50c, and nothing for the 124 call minutes while Twilio is paused.
    expect(await screen.findByLabelText('AI minute rate')).toHaveValue('0.50')
    expect(tile('Estimated').getByText('$99.20')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('shows a finished month as billed rather than as an estimate', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))
    // 198.40 AI minutes at 25c. The sample Twilio minutes are not billed here.
    expect(tile('Estimated').getByText('$49.60')).toBeInTheDocument()

    await user.selectOptions(monthSelect(), LAST_MONTH)

    // A month that has run its course has a bill, and the bill is what the client was charged.
    expect(await screen.findByText('Paid', { selector: '.tile-label' })).toBeInTheDocument()
    expect(screen.queryByText('Estimated', { selector: '.tile-label' })).not.toBeInTheDocument()
    // The calls on that bill say the same thing as the bill - all but the one that came in
    // after the run had charged it, which is on no bill and billable still.
    expect(screen.getAllByText('Paid', { selector: '.pill' }).length).toBeGreaterThan(1)
    expect(screen.getAllByText('Billable')).toHaveLength(1)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('tests a sample key without calling Retell, and names what it found', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))
    await user.click(await screen.findByRole('tab', { name: 'Retell workspace' }))

    await user.type(screen.getByLabelText('Retell workspace API key'), 'key_sample')
    await user.click(screen.getByRole('button', { name: 'Test' }))

    expect(await screen.findByText(/This key can see/)).toBeInTheDocument()
    expect(screen.getByText(/Shared after-hours line/)).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('shows the sample client stuck in the webhook badge trap', async () => {
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Branwynn Destinations' }))
    await user.click(await screen.findByRole('tab', { name: 'Retell workspace' }))

    // Saved, green, and never heard from: the one failure nothing automatic can catch.
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.getByText(/probably not the one with the webhook badge/)).toBeInTheDocument()
  })

  it('sets up the unmapped sample client with a key, which then appears mapped', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Summit Air Solutions' }))

    await user.type(screen.getByLabelText('Retell workspace API key'), 'key_summit_9c40')
    await user.click(screen.getByRole('button', { name: 'Test' }))
    await screen.findByText(/This key can see/)
    await user.click(screen.getByRole('button', { name: 'Save key' }))

    // The setup card is gone: it is a mapped client's page now, with the last four of the key
    // it was given and the agents its workspace turned out to hold.
    await user.click(await screen.findByRole('tab', { name: 'Retell workspace' }))
    expect(await screen.findByText('••••9c40')).toBeInTheDocument()
    expect(
      screen.queryByText('Not set up for billing yet.', { exact: false }),
    ).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '← All clients' }))

    expect(await screen.findByRole('button', { name: 'Summit Air Solutions' })).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
