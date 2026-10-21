import type { OnboardingStatus } from '../types/ghl'
import { apiFetch } from './client'

export function getOnboardingStatus(): Promise<OnboardingStatus> {
  return apiFetch<OnboardingStatus>('/ghl/onboarding/')
}

export function syncLocations(): Promise<OnboardingStatus> {
  return apiFetch<OnboardingStatus>('/ghl/onboarding/sync/', { method: 'POST' })
}
