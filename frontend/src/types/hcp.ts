/** What `/api/hcp/accounts/...` answers with. See `backend/apps/hcp/services/onboarding.py`. */

/** `onboarding_status()['state']`, in the order an account passes through them. */
export type HcpState =
  | 'not_connected'
  | 'needs_employee_mapping'
  | 'needs_calendar_settings'
  | 'ready_to_activate'
  | 'backfilling'
  | 'active'

export type BackfillStatus = 'idle' | 'running' | 'completed' | 'failed'

export type BackfillStage = 'customers' | 'leads' | 'estimates' | 'jobs'

/** Outcome counts for one stage so far: `synced`, `skipped`, `ignored` and `failed`. */
export type StageCounts = Partial<Record<'synced' | 'skipped' | 'ignored' | 'failed', number>>

export interface Backfill {
  status: BackfillStatus
  /** The stage running, or the one a failed backfill stopped at. Empty before the first starts. */
  stage: BackfillStage | ''
  progress: Partial<Record<BackfillStage, StageCounts>>
}

export interface EmployeeMapping {
  id: number
  hcp_employee_id: string
  hcp_employee_name: string
  ghl_user_id: string
  ghl_user_name: string
}

export interface CalendarSettings {
  estimate_calendar_id: string
  estimate_calendar_name: string
  job_calendar_id: string
  job_calendar_name: string
  default_user: EmployeeMapping | null
  estimate_default_user: EmployeeMapping | null
  job_default_user: EmployeeMapping | null
}

export interface HcpAccount {
  hcp_company_id: string
  company_name: string
  company_phone: string
  company_email: string
  timezone: string
  connected_at: string
}

export interface HcpStatus {
  location_id: string
  state: HcpState
  is_active: boolean
  /** Null for a sub-account nobody has connected yet. */
  backfill: Backfill | null
  account: HcpAccount | null
  employee_mappings: EmployeeMapping[]
  calendar_settings: CalendarSettings | null
}

export interface HcpEmployee {
  id: string
  name: string
  email: string
}

export interface GhlUser {
  id: string
  name: string
  email: string
}

export interface EmployeeDirectory {
  hcp_employees: HcpEmployee[]
  ghl_users: GhlUser[]
  mappings: EmployeeMapping[]
}

export interface RoundRobinCalendar {
  id: string
  name: string
}

/** One row of the mapping table as the save endpoint takes it. */
export interface EmployeeMappingRow {
  hcp_employee_id: string
  hcp_employee_name: string
  ghl_user_id: string
  ghl_user_name: string
}

/** The three `*_user` fields are `EmployeeMapping.id`s, not GHL user ids. */
export interface CalendarSettingsFields {
  estimate_calendar_id: string
  job_calendar_id: string
  default_user: number | null
  estimate_default_user: number | null
  job_default_user: number | null
}

/** A GHL user the backend found missing from a calendar's team. */
export interface MissingTeamMember {
  ghl_user_id: string
  ghl_user_name: string
}

/** Why the backend refused a step, in the words it sent. `code` is set for the HCP and GHL
 * failures (`hcp_invalid_api_key`, `hcp_rate_limited`, `hcp_unavailable`, `ghl_unavailable`,
 * `backfill_running`); `missing_team_members` for a calendar save refused over team membership,
 * keyed by `estimate_calendar_id` / `job_calendar_id`. */
export interface HcpErrorBody {
  detail?: string
  code?: string
  missing_team_members?: Partial<Record<'estimate_calendar_id' | 'job_calendar_id', MissingTeamMember[]>>
}
