import { useCallback, useEffect, useState } from 'react'
import { activateHcp, getHcpStatus } from '../../api/hcp'
import { ApiError } from '../../api/client'
import type { Backfill, BackfillStage, HcpStatus, StageCounts } from '../../types/hcp'
import { describeHcpError } from './hcpErrors'
import { CalendarsStep, ConnectStep, EmployeesStep } from './HcpSetupSteps'

/** How often the status is re-read while a backfill runs. */
const POLL_MS = 3000

const STAGES: { key: BackfillStage; label: string }[] = [
  { key: 'customers', label: 'Customers' },
  { key: 'leads', label: 'Leads' },
  { key: 'estimates', label: 'Estimates' },
  { key: 'jobs', label: 'Jobs' },
]

const STEP_OF_STATE: Partial<Record<HcpStatus['state'], number>> = {
  not_connected: 1,
  needs_employee_mapping: 2,
  needs_calendar_settings: 3,
  ready_to_activate: 4,
}

const STEP_TITLES = ['Connect', 'Map employees', 'Calendars', 'Activate']

interface Props {
  locationId: string
  /** The standalone preview never reaches the API: it shows a notice instead of the setup. */
  preview: boolean
  /** Test seam: how often the status is re-read while a backfill runs. */
  pollMs?: number
}

/**
 * Housecall Pro onboarding for one client, one step at a time, in the order the backend's state
 * machine moves through them: connect, map employees, choose calendars, activate. The page holds
 * no state of its own about which step it is on - it is whatever `GET .../status/` says, so a
 * reload, or another admin finishing a step, lands on the right one.
 */
export default function HousecallProTab({ locationId, preview, pollMs = POLL_MS }: Props) {
  if (preview) {
    return (
      <section className="card">
        <h2>Housecall Pro</h2>
        <p className="muted small">
          Housecall Pro setup isn&apos;t available in the standalone preview. It reads and writes
          the real integration, which this page never calls.
        </p>
      </section>
    )
  }
  return <LiveHousecallPro locationId={locationId} pollMs={pollMs} />
}

function LiveHousecallPro({ locationId, pollMs }: { locationId: string; pollMs: number }) {
  const [status, setStatus] = useState<HcpStatus | null>(null)
  const [loadError, setLoadError] = useState(false)
  const [editing, setEditing] = useState<'employees' | 'calendars' | null>(null)

  const refresh = useCallback(async () => {
    try {
      setStatus(await getHcpStatus(locationId))
      setLoadError(false)
    } catch {
      setLoadError(true)
    }
  }, [locationId])

  useEffect(() => {
    let cancelled = false
    getHcpStatus(locationId)
      .then((loaded) => {
        if (!cancelled) setStatus(loaded)
      })
      .catch(() => {
        if (!cancelled) setLoadError(true)
      })
    return () => {
      cancelled = true
    }
  }, [locationId])

  const backfilling = status?.state === 'backfilling'
  useEffect(() => {
    if (!backfilling) return
    const timer = setInterval(() => void refresh(), pollMs)
    return () => clearInterval(timer)
  }, [backfilling, refresh, pollMs])

  if (!status) {
    return (
      <section className="card">
        <h2>Housecall Pro</h2>
        {loadError ? (
          <>
            <div className="banner error" role="alert">
              Could not load the Housecall Pro setup.
            </div>
            <p>
              <button type="button" className="button ghost" onClick={() => void refresh()}>
                Try again
              </button>
            </p>
          </>
        ) : (
          <p className="muted small">Loading…</p>
        )}
      </section>
    )
  }

  // Re-opening an earlier step to change it. Never during a backfill, which is reading the
  // very mappings and calendars an edit would move.
  if (editing && status.state !== 'backfilling') {
    const done = (saved: HcpStatus) => {
      setStatus(saved)
      setEditing(null)
    }
    return (
      <section className="card">
        <h2>Housecall Pro</h2>
        <p className="muted small">{editing === 'employees' ? 'Edit employees' : 'Edit calendars'}</p>
        {editing === 'employees' ? (
          <EmployeesStep locationId={locationId} onDone={done} onCancel={() => setEditing(null)} />
        ) : (
          <CalendarsStep
            locationId={locationId}
            status={status}
            onDone={done}
            onCancel={() => setEditing(null)}
          />
        )}
      </section>
    )
  }

  const step = STEP_OF_STATE[status.state]
  return (
    <section className="card">
      <h2>Housecall Pro</h2>
      {step && (
        <p className="muted small">
          Step {step} of 4 — {STEP_TITLES[step - 1]}
        </p>
      )}
      {status.state === 'not_connected' && (
        <ConnectStep locationId={locationId} onDone={setStatus} />
      )}
      {status.state === 'needs_employee_mapping' && (
        <EmployeesStep locationId={locationId} onDone={setStatus} />
      )}
      {status.state === 'needs_calendar_settings' && (
        <>
          <p>
            <button type="button" className="button ghost" onClick={() => setEditing('employees')}>
              ← Edit employees
            </button>
          </p>
          <CalendarsStep locationId={locationId} status={status} onDone={setStatus} />
        </>
      )}
      {status.state === 'ready_to_activate' && (
        <>
          <ActivateStep locationId={locationId} status={status} onStarted={setStatus} />
          <EditButtons onEdit={setEditing} />
        </>
      )}
      {status.state === 'backfilling' && (
        <>
          <p>Pulling this company&apos;s history from Housecall Pro. This can take a while.</p>
          <StageList backfill={status.backfill} />
          {loadError && (
            <p className="muted small" role="status">
              Could not refresh the progress. Still trying.
            </p>
          )}
          <EditButtons onEdit={setEditing} disabled />
          <p className="muted small">Editing is available once the backfill has finished.</p>
        </>
      )}
      {status.state === 'active' && (
        <>
          <ActiveSummary status={status} />
          <EditButtons onEdit={setEditing} />
        </>
      )}
    </section>
  )
}

/** Re-open an earlier step. The same `PUT` endpoints save it, so nothing new on the backend. */
function EditButtons({
  onEdit,
  disabled,
}: {
  onEdit: (step: 'employees' | 'calendars') => void
  disabled?: boolean
}) {
  return (
    <p className="actions">
      <button
        type="button"
        className="button ghost"
        onClick={() => onEdit('employees')}
        disabled={disabled}
      >
        Edit employees
      </button>
      <button
        type="button"
        className="button ghost"
        onClick={() => onEdit('calendars')}
        disabled={disabled}
      >
        Edit calendars
      </button>
    </p>
  )
}

function ActivateStep({
  locationId,
  status,
  onStarted,
}: {
  locationId: string
  status: HcpStatus
  onStarted: (status: HcpStatus) => void
}) {
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const failed = status.backfill?.status === 'failed'

  async function start() {
    setStarting(true)
    setError(null)
    try {
      onStarted(await activateHcp(locationId))
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Someone else started it between this page loading and the click: show it running.
        onStarted(await getHcpStatus(locationId))
      } else {
        setError(describeHcpError(err, 'Could not start the backfill. Try again.'))
      }
    } finally {
      setStarting(false)
    }
  }

  return (
    <>
      {failed ? (
        <div className="banner error" role="alert">
          The last backfill stopped
          {status.backfill?.stage ? ` while pulling ${status.backfill.stage}` : ''}. Records
          already pulled are kept, so trying again picks up cheaply.
        </div>
      ) : (
        <p>
          Everything is set up. Activating pulls this company&apos;s customers, leads, estimates
          and jobs from Housecall Pro into GoHighLevel, then switches the sync on.
        </p>
      )}
      {failed && <StageList backfill={status.backfill} />}
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      <p>
        <button type="button" className="button" onClick={start} disabled={starting}>
          {starting ? 'Starting…' : failed ? 'Retry activation' : 'Activate'}
        </button>
      </p>
    </>
  )
}

function ActiveSummary({ status }: { status: HcpStatus }) {
  const { account, calendar_settings: calendars } = status
  return (
    <>
      <p>
        <span className="pill ok">Active</span>
      </p>
      <dl className="facts">
        <dt>Housecall Pro company</dt>
        <dd>{account?.company_name || <span className="muted">Unnamed</span>}</dd>
        <dt>Employees mapped</dt>
        <dd>{status.employee_mappings.length}</dd>
        <dt>Estimate calendar</dt>
        <dd>{calendars?.estimate_calendar_name || calendars?.estimate_calendar_id}</dd>
        <dt>Job calendar</dt>
        <dd>{calendars?.job_calendar_name || calendars?.job_calendar_id}</dd>
      </dl>
      <StageList backfill={status.backfill} />
    </>
  )
}

/** Each backfill stage with where it is and what it has done so far. */
function StageList({ backfill }: { backfill: Backfill | null }) {
  if (!backfill) return null
  const current = STAGES.findIndex((stage) => stage.key === backfill.stage)
  return (
    <ul className="stage-list" aria-label="Backfill progress">
      {STAGES.map((stage, index) => {
        const counts = backfill.progress[stage.key]
        const state = stageState(backfill, index, current, counts)
        return (
          <li key={stage.key}>
            <span className="stage-name">{stage.label}</span>
            <span className={`pill ${state.tone}`}>{state.label}</span>
            {counts && <span className="muted small">{describeCounts(counts)}</span>}
          </li>
        )
      })}
    </ul>
  )
}

function stageState(
  backfill: Backfill,
  index: number,
  current: number,
  counts: StageCounts | undefined,
): { label: string; tone: string } {
  if (backfill.status === 'completed') return { label: 'Done', tone: 'ok' }
  if (index < current) return { label: 'Done', tone: 'ok' }
  if (index === current) {
    return backfill.status === 'failed'
      ? { label: 'Stopped', tone: 'err' }
      : { label: 'Running', tone: 'warn' }
  }
  return { label: counts ? 'Done' : 'Waiting', tone: counts ? 'ok' : '' }
}

function describeCounts(counts: StageCounts): string {
  const parts = (['synced', 'skipped', 'ignored', 'failed'] as const)
    .filter((key) => counts[key])
    .map((key) => `${counts[key]} ${key}`)
  return parts.length > 0 ? parts.join(', ') : 'nothing yet'
}
