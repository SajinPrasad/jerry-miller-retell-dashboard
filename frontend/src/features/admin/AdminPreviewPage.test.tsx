import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdminPreviewPage from './AdminPreviewPage'
import {
  resetPreviewCardRequests,
  resetPreviewMappings,
  resetPreviewRates,
  resetPreviewRetries,
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
    resetPreviewRetries()
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

    // The agent's own name, not its raw Retell id - on the Agent mapping tab.
    await user.click(screen.getByRole('tab', { name: 'Agent mapping' }))
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
    await screen.findByRole('tab', { name: 'Overview & billing' })

    // The sample data still carries this client's daily Twilio rows - the preview simply does
    // not show them while rebilling is paused.
    expect(screen.queryByText('Twilio calls by day')).not.toBeInTheDocument()
    expect(screen.queryByText('Call minutes', { selector: '.tile-label' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Call minute rate')).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Agent mapping' }))

    // The sample data still carries this client's SID and its phone numbers too - hidden here
    // for the same reason.
    expect(
      screen.queryByRole('button', { name: 'ACdemopolarpeak00000000000000002' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Add a phone number')).not.toBeInTheDocument()
    // Still the one thing an admin can map here.
    expect(screen.getByRole('button', { name: 'Add a Retell agent' })).toBeInTheDocument()
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

  it('searches the sample agent list without calling Retell', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))
    await user.click(await screen.findByRole('tab', { name: 'Agent mapping' }))
    await user.click(screen.getByRole('button', { name: 'Add a Retell agent' }))

    await user.type(screen.getByLabelText('Search Retell agents'), 'after-hours')

    expect(await screen.findByText('Shared after-hours line')).toBeInTheDocument()
    expect(screen.queryByText('Branwynn Destinations — main line')).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('marks the failed and retrying sample clients on the preview list and lets retry settle them', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)

    const polarRow = (
      await screen.findByRole('button', { name: 'Polar Peak Heating & Air' })
    ).closest('tr') as HTMLElement
    expect(within(polarRow).getByText('Payment failed')).toHaveClass('pill', 'err')

    const harborRow = screen
      .getByRole('button', { name: 'Harbor Heat & Cool' })
      .closest('tr') as HTMLElement
    expect(within(harborRow).getByText('In Retry')).toHaveClass('pill', 'warn')

    // Harbor Heat & Cool already shows In Retry on initial open without changing months.
    await user.click(screen.getByRole('button', { name: 'Harbor Heat & Cool' }))
    expect(await screen.findByText('In Retry', { selector: '.tile-label' })).toHaveClass(
      'pill',
      'warn',
    )
    expect(screen.getByText('Retry 2 of 3')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry payment' })).toBeInTheDocument()

    // Polar Peak Heating & Air shows Failed, calls paused, and Retry payment straight away.
    await user.click(screen.getByRole('button', { name: '← All clients' }))
    await user.click(await screen.findByRole('button', { name: 'Polar Peak Heating & Air' }))

    expect(await screen.findByText('Failed', { selector: '.tile-label' })).toHaveClass('pill', 'err')
    expect(screen.getByText('card_declined')).toBeInTheDocument()
    expect(screen.getByText(/Calls paused/)).toBeInTheDocument()
    expect(screen.getByText(/Automatic retries have ended/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Retry payment' }))

    expect(await screen.findByText('Paid', { selector: '.tile-label' })).toBeInTheDocument()
    expect(screen.queryByText(/Calls paused/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry payment' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '← All clients' }))
    const updatedPolarRow = (
      await screen.findByRole('button', { name: 'Polar Peak Heating & Air' })
    ).closest('tr') as HTMLElement
    expect(within(updatedPolarRow).queryByText('Payment failed')).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('leaves an agent mapped to another sample client out of the picker', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Summit Air Solutions' }))
    await user.click(screen.getByRole('button', { name: 'Add a Retell agent' }))

    // The spare and this client's own agent are free; Cool Breeze's is spoken for, and an agent
    // on two mappings would bill whichever client the backend's own lookup answered with first.
    expect(await screen.findByText('Shared after-hours line')).toBeInTheDocument()
    expect(screen.getByText('Summit Air Solutions — main line')).toBeInTheDocument()
    expect(screen.queryByText('Cool Breeze HVAC — main line')).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('offers an agent again once it is taken off the client that had it', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))
    await user.click(await screen.findByRole('tab', { name: 'Agent mapping' }))
    await user.click(screen.getByRole('button', { name: 'Remove Cool Breeze HVAC — main line' }))
    await user.click(screen.getByRole('button', { name: 'Save mapping' }))

    await user.click(screen.getByRole('button', { name: '← All clients' }))
    await user.click(await screen.findByRole('button', { name: 'Summit Air Solutions' }))
    await user.click(screen.getByRole('button', { name: 'Add a Retell agent' }))

    // Freed on its own client, so it is on offer to the rest - no refresh, no reload.
    expect(await screen.findByText('Cool Breeze HVAC — main line')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('sets up the unmapped sample client, which then appears mapped in the list', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Summit Air Solutions' }))

    await user.click(screen.getByRole('button', { name: 'Add a Retell agent' }))
    await user.click(await screen.findByRole('button', { name: /Summit Air Solutions — main line/ }))
    await user.click(screen.getByRole('button', { name: 'Save mapping' }))

    // The setup card is gone: it is a mapped client's page now, with the agent it was given -
    // by name, not by its raw Retell id - on the Agent mapping tab.
    await user.click(await screen.findByRole('tab', { name: 'Agent mapping' }))
    expect(await screen.findByText('Summit Air Solutions — main line')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '← All clients' }))

    expect(await screen.findByRole('button', { name: 'Summit Air Solutions' })).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
