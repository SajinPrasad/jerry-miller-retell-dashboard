import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AdminPreviewPage from './AdminPreviewPage'
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

  it('never shows the unmapped sample client as clickable', async () => {
    render(<AdminPreviewPage />)

    expect(await screen.findByText('Summit Air Solutions')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Summit Air Solutions' })).not.toBeInTheDocument()
  })
})
