import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdminPreviewPage from './AdminPreviewPage'
import { resetPreviewMappings } from './demoData'
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

    expect(await screen.findByText('agent_polar_peak')).toBeInTheDocument()
    expect(screen.getByText('Rates')).toBeInTheDocument()
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

  it('picks a sample Twilio subaccount in the preview, and it sticks on reopen', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Polar Peak Heating & Air' }))

    // The SID Polar Peak is on today. The picker offers the sample subaccounts, not Twilio's.
    await user.click(await screen.findByRole('button', { name: 'ACdemopolarpeak00000000000000002' }))
    await user.click(await screen.findByRole('button', { name: /Cool Breeze HVAC/ }))
    await user.click(screen.getByRole('button', { name: 'Save mapping' }))

    await user.click(screen.getByRole('button', { name: '← All clients' }))
    await user.click(await screen.findByRole('button', { name: 'Polar Peak Heating & Air' }))

    expect(
      await screen.findByRole('button', { name: 'ACdemocoolbreeze0000000000000001' }),
    ).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('searches the sample agent list without calling Retell', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const user = userEvent.setup()

    render(<AdminPreviewPage />)
    await user.click(await screen.findByRole('button', { name: 'Cool Breeze HVAC' }))
    await user.click(screen.getByRole('button', { name: 'Add a Retell agent' }))

    await user.type(screen.getByLabelText('Search Retell agents'), 'after-hours')

    expect(await screen.findByText('Shared after-hours line')).toBeInTheDocument()
    expect(screen.queryByText('Branwynn Destinations — main line')).not.toBeInTheDocument()
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

    // The setup card is gone: it is a mapped client's page now, with the agent it was given.
    expect(await screen.findByText('agent_summit_air')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '← All clients' }))

    expect(await screen.findByRole('button', { name: 'Summit Air Solutions' })).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
