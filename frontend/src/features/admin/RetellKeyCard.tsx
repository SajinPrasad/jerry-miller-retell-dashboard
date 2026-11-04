import { useState } from 'react'
import { saveRetellKey, testRetellKey } from '../../api/admin'
import type { RetellAgent, RetellWorkspaceSummary } from '../../types/admin'
import { previewRetellAgents, savePreviewRetellKey } from './demoData'
import { formatDateTime } from './format'

interface Props {
  locationId: string
  clientName: string
  /** Null while nobody has given this client a key. */
  workspace: RetellWorkspaceSummary | null
  /** The agents currently known for this client, named off their own workspace. */
  agents: RetellAgent[]
  /** Agency scope only. A sub-account sees the status and nothing it could change. */
  canEdit: boolean
  /** The standalone preview never reaches the admin API or Retell. */
  preview: boolean
  /** False inside the client page's own tab, where the tab is already the heading. */
  heading?: boolean
  onSaved: () => void
}

/**
 * One client's own Retell workspace: the key we read it with, where their calls arrive, and
 * whether any ever have.
 *
 * Every client has their own Retell workspace now, with its own agents, its own phone numbers
 * and its own API key. That key is the only thing that can read their calls, and **the same
 * key is what signs their webhooks** - which is why their webhook URL has to carry their id,
 * and why pasting a key here is what sets a client up for billing at all.
 *
 * Two failures this card exists to make visible, because nothing in the code can see either:
 *
 * 1. **The wrong key from the right workspace.** Retell signs a workspace's webhooks with one
 *    specific key - the one carrying the webhook badge in its dashboard. Any other key of that
 *    workspace passes Test, reads the API perfectly well, and then fails every signature
 *    silently. Hence the path guidance next to the field, and the warning once a key has been
 *    saved for a while with no webhook ever received.
 * 2. **The right kind of key from the wrong workspace.** It authenticates, lists agents, and
 *    bills this client for another client's calls. That is why Test shows the agent names it
 *    found and asks for them to be checked, rather than answering a bare "key works".
 */
export default function RetellKeyCard({
  locationId,
  clientName,
  workspace,
  agents,
  canEdit,
  preview,
  heading = true,
  onSaved,
}: Props) {
  const [apiKey, setApiKey] = useState('')
  const [workspaceName, setWorkspaceName] = useState(workspace?.workspace_name ?? '')
  // The agents the last test found, which is what the admin confirms before saving. Null
  // until a test has passed; cleared whenever the key is edited, so a confirmation can never
  // belong to a different key than the one about to be saved.
  const [found, setFound] = useState<RetellAgent[] | null>(null)
  const [busy, setBusy] = useState<'testing' | 'saving' | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function handleTest() {
    setBusy('testing')
    setError(null)
    setFound(null)
    try {
      const result = preview
        ? { agents: previewRetellAgents() }
        : await testRetellKey(locationId, apiKey.trim())
      setFound(result.agents)
    } catch {
      setError(
        'Retell would not accept this key. Check it is the workspace API key with the webhook badge.',
      )
    } finally {
      setBusy(null)
    }
  }

  async function handleSave() {
    setBusy('saving')
    setError(null)
    try {
      if (preview) savePreviewRetellKey(locationId, apiKey.trim(), workspaceName.trim())
      else await saveRetellKey(locationId, apiKey.trim(), workspaceName.trim())
      setApiKey('')
      setFound(null)
      onSaved()
    } catch {
      setError('Could not save the key. Try again.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="card workspace-card">
      <header className="ws-head">
        {heading && <h2>Retell workspace</h2>}
        <StatusPill workspace={workspace} />
      </header>
      {workspace ? (
        <p className="muted small ws-status-note">
          <WorkspaceNote workspace={workspace} />
        </p>
      ) : (
        <p className="muted small ws-status-note">
          Nothing is recorded for this client until their workspace key is saved.
        </p>
      )}

      {workspace && (
        <>
          <div className="ws-grid">
            <Field label="API key">
              <code className="ws-key">{`••••${workspace.key_last4}`}</code>
              {workspace.last_checked_at && (
                <span className="muted small">
                  {` checked ${formatDateTime(workspace.last_checked_at)} UTC`}
                </span>
              )}
            </Field>
            <Field label="Workspace">
              {workspace.workspace_name || <span className="muted">Unnamed</span>}
            </Field>
          </div>

          <Field label="Webhook URL" block>
            <CopyField value={workspace.webhook_url} label="Copy the webhook URL" />
            <p className="muted small ws-hint">
              Paste this into this workspace in Retell under <strong>Settings → Webhooks</strong>.
            </p>
          </Field>
        </>
      )}

      <Field label="Agents" block>
        {agents.length > 0 ? (
          <ul className="chip-list">
            {agents.map((agent) => (
              <li className="chip static" key={agent.agent_id}>
                {agent.agent_name || agent.agent_id}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted small">
            {workspace ? 'None in this workspace yet.' : 'No key, so nothing to read.'}
          </p>
        )}
      </Field>

      {canEdit && (
        <div className="ws-setup">
          <div className="key-editor">
            <label className="key-field">
              <span className="field-label">
                {workspace ? 'Replace the key' : 'Workspace API key'}
              </span>
              <input
                type="password"
                value={apiKey}
                autoComplete="off"
                placeholder="key_…"
                aria-label="Retell workspace API key"
                aria-describedby="retell-key-note"
                onChange={(event) => {
                  setApiKey(event.target.value)
                  setFound(null)
                }}
              />
            </label>
            <label className="key-field">
              <span className="field-label">Workspace name</span>
              <input
                type="text"
                value={workspaceName}
                placeholder={clientName}
                aria-label="Retell workspace name"
                onChange={(event) => setWorkspaceName(event.target.value)}
              />
            </label>
            <div className="key-actions">
              <button
                type="button"
                className="button ghost"
                onClick={handleTest}
                disabled={!apiKey.trim() || busy !== null}
              >
                {busy === 'testing' ? 'Testing…' : 'Test'}
              </button>
              <button
                type="button"
                className="button"
                onClick={handleSave}
                disabled={found === null || busy !== null}
              >
                {busy === 'saving' ? 'Saving…' : 'Save key'}
              </button>
            </div>
          </div>
          <p className="muted small" id="retell-key-note">
            In Retell: <strong>Settings → API Keys</strong> —{' '}
            <strong>copy the key with the webhook badge</strong>. Any other key tests fine here
            and then rejects every call silently.
          </p>
        </div>
      )}

      {found && (
        <div className="banner warn" role="status">
          <strong>
            {found.length > 0
              ? `This key can see ${found.length} agent${found.length === 1 ? '' : 's'}:`
              : 'This key works, but the workspace has no agents in it yet.'}
          </strong>{' '}
          {found.length > 0 && found.map((agent) => agent.agent_name || agent.agent_id).join(', ')}{' '}
          Check {found.length > 0 ? 'these belong' : 'this is the right workspace'} to{' '}
          {clientName || 'this client'} before saving — another client&apos;s key bills them for
          someone else&apos;s calls.
        </div>
      )}

      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
    </section>
  )
}

/** A labelled value, with the label as a quiet micro-caption above whatever it names. */
function Field({
  label,
  block,
  children,
}: {
  label: string
  /** Full width, for things that are read or pasted rather than scanned in a pair. */
  block?: boolean
  children: React.ReactNode
}) {
  return (
    <div className={block ? 'ws-field block' : 'ws-field'}>
      <span className="field-label">{label}</span>
      <div className="ws-value">{children}</div>
    </div>
  )
}

/** Where this client stands in one word, which is what the list of clients is scanned for. */
function StatusPill({ workspace }: { workspace: RetellWorkspaceSummary | null }) {
  if (!workspace) return <span className="pill warn">No key</span>
  if (workspace.status === 'key_invalid') return <span className="pill err">Key not working</span>
  return <span className="pill ok">Active</span>
}

/**
 * The sentence under the status, and the only place the badge trap is visible.
 *
 * "Active, and no webhook has ever arrived" is not a quiet client - a quiet client would have
 * no calls either, and that shows up as a month billed at zero. It is almost always the wrong
 * key of the right workspace.
 */
function WorkspaceNote({ workspace }: { workspace: RetellWorkspaceSummary }) {
  if (workspace.status === 'key_invalid') {
    return <>Retell refused this key on the last check. Calls are not being recorded.</>
  }
  if (!workspace.last_webhook_at) {
    return (
      <>
        No webhook has arrived yet. If this client is taking calls, the saved key is probably not
        the one with the webhook badge.
      </>
    )
  }
  return <>{`Last webhook ${formatDateTime(workspace.last_webhook_at)} UTC.`}</>
}

/** A value to be pasted somewhere else, with the one button that matters next to it. */
function CopyField({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // No clipboard permission, or an insecure origin. The URL is on screen either way, so
      // there is nothing to report - selecting it by hand still works.
    }
  }

  return (
    <span className="copy-field">
      <code className="copy-value">{value}</code>
      <button type="button" className="icon-button" onClick={copy} aria-label={label}>
        {copied ? <CheckIcon /> : <ClipboardIcon />}
      </button>
      {copied && (
        <span className="copy-flag" role="status">
          Copied
        </span>
      )}
    </span>
  )
}

function ClipboardIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
      <rect x="9" y="9" width="11" height="12" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1" />
    </svg>
  )
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
      <path d="m4 12.5 5 5L20 6.5" />
    </svg>
  )
}
