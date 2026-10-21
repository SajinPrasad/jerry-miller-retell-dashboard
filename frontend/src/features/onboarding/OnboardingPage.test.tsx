import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { OnboardingStatus } from '../../types/ghl'
import OnboardingPage from './OnboardingPage'

const disconnected: OnboardingStatus = {
  install_url: 'https://marketplace.example.com/oauth/chooselocation?x=1',
  agency: null,
  locations: [],
}

const connected: OnboardingStatus = {
  ...disconnected,
  agency: {
    company_id: 'comp1',
    location_id: '',
    name: '',
    is_active: true,
    expires_at: '2026-10-20T10:00:00Z',
  },
  locations: [
    {
      company_id: 'comp1',
      location_id: 'loc1',
      name: 'Cool Breeze HVAC',
      is_active: true,
      expires_at: '2026-10-20T10:00:00Z',
    },
    {
      company_id: 'comp1',
      location_id: 'loc2',
      name: 'Old Client',
      is_active: false,
      expires_at: '2026-10-20T10:00:00Z',
    },
  ],
}

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

function renderPage(search = '') {
  return render(
    <MemoryRouter initialEntries={[`/onboarding${search}`]}>
      <OnboardingPage />
    </MemoryRouter>,
  )
}

describe('OnboardingPage', () => {
  const fetchMock = vi.fn()

  beforeEach(() => vi.stubGlobal('fetch', fetchMock))

  afterEach(() => {
    fetchMock.mockReset()
    vi.unstubAllGlobals()
  })

  it('links the connect button to the install url when disconnected', async () => {
    fetchMock.mockReturnValue(jsonResponse(disconnected))
    renderPage()

    const link = await screen.findByRole('link', { name: 'Connect to GoHighLevel' })
    expect(link).toHaveAttribute('href', disconnected.install_url)
    expect(screen.queryByText('Client sub-accounts')).not.toBeInTheDocument()
  })

  it('shows the agency and its sub-accounts', async () => {
    fetchMock.mockReturnValue(jsonResponse(connected))
    renderPage('?status=connected')

    expect(await screen.findByText('Cool Breeze HVAC')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Agency connected.')
    expect(screen.getByText('comp1')).toBeInTheDocument()
    expect(screen.getByText('Active')).toBeInTheDocument()
    expect(screen.getByText('Uninstalled')).toBeInTheDocument()
  })

  it('shows the callback error', async () => {
    fetchMock.mockReturnValue(jsonResponse(disconnected))
    renderPage('?error=install_cancelled')

    expect(await screen.findByRole('alert')).toHaveTextContent('cancelled')
  })

  it('refreshes sub-accounts', async () => {
    const user = userEvent.setup()
    fetchMock.mockReturnValueOnce(jsonResponse(connected)).mockReturnValueOnce(
      jsonResponse({
        ...connected,
        locations: [{ ...connected.locations[0], location_id: 'loc3', name: 'Branwynn' }],
      }),
    )
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Refresh sub-accounts' }))

    expect(await screen.findByText('Branwynn')).toBeInTheDocument()
    expect(fetchMock.mock.calls[1][0]).toBe('/api/ghl/onboarding/sync/')
    expect(fetchMock.mock.calls[1][1].method).toBe('POST')
  })

  it('shows an error when the refresh fails', async () => {
    const user = userEvent.setup()
    fetchMock
      .mockReturnValueOnce(jsonResponse(connected))
      .mockReturnValueOnce(jsonResponse({ detail: 'GHL request failed.' }, 502))
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Refresh sub-accounts' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not refresh')
  })
})
