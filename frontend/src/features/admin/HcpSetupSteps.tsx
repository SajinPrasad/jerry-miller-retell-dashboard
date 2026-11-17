import { useEffect, useState } from 'react'
import {
  connectHcp,
  getCalendars,
  getEmployees,
  saveCalendarSettings,
  saveEmployeeMappings,
} from '../../api/hcp'
import { ApiError } from '../../api/client'
import type {
  EmployeeDirectory,
  HcpErrorBody,
  HcpStatus,
  RoundRobinCalendar,
} from '../../types/hcp'
import { describeHcpError } from './hcpErrors'

interface StepProps {
  locationId: string
  onDone: (status: HcpStatus) => void
  /** Set when the step is being re-opened to edit: leaves it without saving. */
  onCancel?: () => void
}

/** Step 1: paste the company's Housecall Pro API key. */
export function ConnectStep({ locationId, onDone }: StepProps) {
  const [apiKey, setApiKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function connect() {
    setSaving(true)
    setError(null)
    try {
      onDone(await connectHcp(locationId, apiKey))
    } catch (err) {
      setError(describeHcpError(err, 'Could not connect. Try again.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mapping-editor">
      <p className="muted small">
        Generate a key in Housecall Pro under My Apps → API Key Management. It is shown once, and
        is stored encrypted.
      </p>
      <label className="mapping-field">
        <span className="tile-label">Housecall Pro API key</span>
        <input
          type="password"
          autoComplete="off"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
        />
      </label>
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      <div>
        <button
          type="button"
          className="button"
          onClick={connect}
          disabled={saving || !apiKey.trim()}
        >
          {saving ? 'Connecting…' : 'Connect'}
        </button>
      </div>
    </div>
  )
}

/** Step 2: match each Housecall Pro employee to the GoHighLevel user their work is assigned to. */
export function EmployeesStep({ locationId, onDone, onCancel }: StepProps) {
  const [directory, setDirectory] = useState<EmployeeDirectory | null>(null)
  // HCP employee id -> GHL user id, `''` for an employee who is not mapped.
  const [choices, setChoices] = useState<Record<string, string>>({})
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getEmployees(locationId)
      .then((loaded) => {
        if (cancelled) return
        setDirectory(loaded)
        setChoices(
          Object.fromEntries(loaded.mappings.map((row) => [row.hcp_employee_id, row.ghl_user_id])),
        )
      })
      .catch((err) => {
        if (!cancelled) setLoadError(describeHcpError(err, 'Could not load the employees.'))
      })
    return () => {
      cancelled = true
    }
  }, [locationId])

  if (loadError) {
    return (
      <div className="banner error" role="alert">
        {loadError}
      </div>
    )
  }
  if (!directory) return <p className="muted small">Loading employees…</p>

  const taken = new Set(Object.values(choices).filter(Boolean))
  const mapped = directory.hcp_employees.filter((employee) => choices[employee.id])

  async function save() {
    if (!directory) return
    setSaving(true)
    setError(null)
    try {
      onDone(
        await saveEmployeeMappings(
          locationId,
          mapped.map((employee) => {
            const user = directory.ghl_users.find((candidate) => candidate.id === choices[employee.id])
            return {
              hcp_employee_id: employee.id,
              hcp_employee_name: employee.name,
              ghl_user_id: choices[employee.id],
              ghl_user_name: user?.name ?? '',
            }
          }),
        ),
      )
    } catch (err) {
      setError(describeHcpError(err, 'Could not save the mappings. Try again.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mapping-editor wide">
      <p className="muted small">
        Choose the GoHighLevel user each Housecall Pro employee&apos;s appointments go to. Each
        user can be chosen once. Employees left unmapped are not assigned work.
      </p>
      {directory.hcp_employees.length === 0 ? (
        <p className="muted">Housecall Pro has no employees for this company.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Housecall Pro employee</th>
              <th>GoHighLevel user</th>
            </tr>
          </thead>
          <tbody>
            {directory.hcp_employees.map((employee) => (
              <tr key={employee.id}>
                <td>{employee.name}</td>
                <td>
                  <select
                    className="hcp-select"
                    aria-label={`GoHighLevel user for ${employee.name}`}
                    value={choices[employee.id] ?? ''}
                    onChange={(event) =>
                      setChoices((current) => ({ ...current, [employee.id]: event.target.value }))
                    }
                  >
                    <option value="">Not mapped</option>
                    {directory.ghl_users.map((user) => (
                      <option
                        key={user.id}
                        value={user.id}
                        disabled={taken.has(user.id) && choices[employee.id] !== user.id}
                      >
                        {user.name || user.email}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      <div className="actions">
        <button
          type="button"
          className="button"
          onClick={save}
          disabled={saving || mapped.length === 0}
        >
          {saving ? 'Saving…' : 'Save mappings'}
        </button>
        {onCancel && (
          <button type="button" className="button ghost" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </div>
  )
}

/** Step 3: the two round-robin calendars, and who an unassigned appointment is booked to. */
export function CalendarsStep({
  locationId,
  status,
  onDone,
  onCancel,
}: StepProps & { status: HcpStatus }) {
  const [calendars, setCalendars] = useState<RoundRobinCalendar[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const saved = status.calendar_settings
  const [estimateId, setEstimateId] = useState(saved?.estimate_calendar_id ?? '')
  const [jobId, setJobId] = useState(saved?.job_calendar_id ?? '')
  // Employee mapping ids, as strings for the select; '' is "none".
  const [defaultUser, setDefaultUser] = useState(idOf(saved?.default_user))
  const [estimateUser, setEstimateUser] = useState(idOf(saved?.estimate_default_user))
  const [jobUser, setJobUser] = useState(idOf(saved?.job_default_user))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [missing, setMissing] = useState<HcpErrorBody['missing_team_members']>()

  useEffect(() => {
    let cancelled = false
    getCalendars(locationId)
      .then((loaded) => {
        if (!cancelled) setCalendars(loaded)
      })
      .catch((err) => {
        if (!cancelled) setLoadError(describeHcpError(err, 'Could not load the calendars.'))
      })
    return () => {
      cancelled = true
    }
  }, [locationId])

  if (loadError) {
    return (
      <div className="banner error" role="alert">
        {loadError}
      </div>
    )
  }
  if (!calendars) return <p className="muted small">Loading calendars…</p>

  const calendarName = (id: string) => calendars.find((calendar) => calendar.id === id)?.name ?? id
  const ready = estimateId && jobId && defaultUser

  async function save() {
    setSaving(true)
    setError(null)
    setMissing(undefined)
    try {
      onDone(
        await saveCalendarSettings(locationId, {
          estimate_calendar_id: estimateId,
          job_calendar_id: jobId,
          default_user: Number(defaultUser),
          estimate_default_user: estimateUser ? Number(estimateUser) : null,
          job_default_user: jobUser ? Number(jobUser) : null,
        }),
      )
    } catch (err) {
      const body = err instanceof ApiError ? ((err.body ?? {}) as HcpErrorBody) : {}
      if (body.missing_team_members) setMissing(body.missing_team_members)
      setError(describeHcpError(err, 'Could not save the calendar settings. Try again.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mapping-editor">
      <p className="muted small">
        Estimates and jobs are booked on two different round-robin calendars. Every mapped user
        must be on the team of both.
      </p>
      {calendars.length < 2 && (
        <div className="banner warn" role="status">
          This sub-account needs at least two round-robin calendars in GoHighLevel.
        </div>
      )}
      <CalendarSelect
        label="Estimate calendar"
        value={estimateId}
        onChange={setEstimateId}
        calendars={calendars}
        otherChoice={jobId}
      />
      <CalendarSelect
        label="Job calendar"
        value={jobId}
        onChange={setJobId}
        calendars={calendars}
        otherChoice={estimateId}
      />
      <UserSelect
        label="Default user"
        value={defaultUser}
        onChange={setDefaultUser}
        mappings={status.employee_mappings}
        blank="Choose a user"
      />
      <p className="muted small">
        Appointments that arrive with no technician assigned are booked to the default user.
      </p>
      <UserSelect
        label="Estimate calendar user (optional)"
        value={estimateUser}
        onChange={setEstimateUser}
        mappings={status.employee_mappings}
        blank="Use the default user"
      />
      <UserSelect
        label="Job calendar user (optional)"
        value={jobUser}
        onChange={setJobUser}
        mappings={status.employee_mappings}
        blank="Use the default user"
      />
      {error && (
        <div className="banner error" role="alert">
          <p>{error}</p>
          {missing && (
            <ul>
              {(['estimate_calendar_id', 'job_calendar_id'] as const).map((field) => {
                const users = missing[field]
                if (!users || users.length === 0) return null
                const calendarId = field === 'estimate_calendar_id' ? estimateId : jobId
                return (
                  <li key={field}>
                    Add {users.map((user) => user.ghl_user_name || user.ghl_user_id).join(', ')} to
                    the team on {calendarName(calendarId)}.
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}
      <div className="actions">
        <button type="button" className="button" onClick={save} disabled={saving || !ready}>
          {saving ? 'Saving…' : 'Save calendars'}
        </button>
        {onCancel && (
          <button type="button" className="button ghost" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </div>
  )
}

function idOf(mapping: { id: number } | null | undefined): string {
  return mapping ? String(mapping.id) : ''
}

function CalendarSelect({
  label,
  value,
  onChange,
  calendars,
  otherChoice,
}: {
  label: string
  value: string
  onChange: (id: string) => void
  calendars: RoundRobinCalendar[]
  /** The other calendar's choice: the two must differ, so it is not offered here. */
  otherChoice: string
}) {
  return (
    <label className="mapping-field">
      <span className="tile-label">{label}</span>
      <select
        className="hcp-select"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Choose a calendar</option>
        {calendars.map((calendar) => (
          <option key={calendar.id} value={calendar.id} disabled={calendar.id === otherChoice}>
            {calendar.name}
          </option>
        ))}
      </select>
    </label>
  )
}

function UserSelect({
  label,
  value,
  onChange,
  mappings,
  blank,
}: {
  label: string
  value: string
  onChange: (id: string) => void
  mappings: HcpStatus['employee_mappings']
  blank: string
}) {
  return (
    <label className="mapping-field">
      <span className="tile-label">{label}</span>
      <select
        className="hcp-select"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{blank}</option>
        {mappings.map((mapping) => (
          <option key={mapping.id} value={mapping.id}>
            {mapping.ghl_user_name || mapping.ghl_user_id} ({mapping.hcp_employee_name})
          </option>
        ))}
      </select>
    </label>
  )
}
