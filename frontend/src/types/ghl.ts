export interface GhlAccount {
  company_id: string
  location_id: string
  name: string
  is_active: boolean
  expires_at: string
}

export interface OnboardingStatus {
  install_url: string
  agency: GhlAccount | null
  locations: GhlAccount[]
}
