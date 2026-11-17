import type {
  CalendarSettingsFields,
  EmployeeDirectory,
  EmployeeMappingRow,
  HcpStatus,
  RoundRobinCalendar,
} from '../types/hcp'
import { apiFetch } from './client'

function accountPath(locationId: string, step: string): string {
  return `/hcp/accounts/${encodeURIComponent(locationId)}/${step}/`
}

export function getHcpStatus(locationId: string): Promise<HcpStatus> {
  return apiFetch<HcpStatus>(accountPath(locationId, 'status'))
}

/** Step 1. The key is checked against Housecall Pro before anything is stored. */
export function connectHcp(locationId: string, apiKey: string): Promise<HcpStatus> {
  return apiFetch<HcpStatus>('/hcp/accounts/', {
    method: 'POST',
    body: JSON.stringify({ location_id: locationId, api_key: apiKey }),
  })
}

/** Step 2's two directories, read live from both systems, plus whatever is already mapped. */
export function getEmployees(locationId: string): Promise<EmployeeDirectory> {
  return apiFetch<EmployeeDirectory>(accountPath(locationId, 'employees'))
}

/** Replaces the whole mapping table: an employee left out of `mappings` is unmapped. */
export function saveEmployeeMappings(
  locationId: string,
  mappings: EmployeeMappingRow[],
): Promise<HcpStatus> {
  return apiFetch<HcpStatus>(accountPath(locationId, 'employee-mappings'), {
    method: 'PUT',
    body: JSON.stringify({ mappings }),
  })
}

export async function getCalendars(locationId: string): Promise<RoundRobinCalendar[]> {
  const body = await apiFetch<{ calendars: RoundRobinCalendar[] }>(
    accountPath(locationId, 'calendars'),
  )
  return body.calendars
}

/** Step 3. Refused, naming who to add where, when a mapped user is not on a calendar's team. */
export function saveCalendarSettings(
  locationId: string,
  settings: CalendarSettingsFields,
): Promise<HcpStatus> {
  return apiFetch<HcpStatus>(accountPath(locationId, 'calendar-settings'), {
    method: 'PUT',
    body: JSON.stringify(settings),
  })
}

/** Step 4. Answers 202 at once; the backfill runs in the background, so poll `getHcpStatus`. */
export function activateHcp(locationId: string): Promise<HcpStatus> {
  return apiFetch<HcpStatus>(accountPath(locationId, 'activate'), { method: 'POST' })
}
