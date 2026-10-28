import { useState } from 'react'
import { saveMapping } from '../../api/admin'
import { ApiError } from '../../api/client'
import type { MappingFields, MappingResult } from '../../types/admin'
import { savePreviewMapping } from './demoData'

interface Props {
  locationId: string
  clientName: string
  initial: MappingFields
  /** The standalone preview never reaches the admin API: saving here only updates the sample
   * data held for this tab, so the workflow can still be demonstrated end to end. */
  preview: boolean
  onSaved: (result: MappingResult) => void
}

/**
 * The one thing an agency admin can write on a client's page: their Retell agents, their backup
 * phone numbers, and their Twilio subaccount. Rates and billing mode have their own rules and
 * are not edited here.
 */
export default function MappingEditor({ locationId, clientName, initial, preview, onSaved }: Props) {
  const [twilioSid, setTwilioSid] = useState(initial.twilio_subaccount_sid)
  const [agentIds, setAgentIds] = useState(initial.retell_agent_ids)
  const [phoneNumbers, setPhoneNumbers] = useState(initial.phone_numbers)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSave() {
    setSaving(true)
    setError(null)
    const fields: MappingFields = {
      twilio_subaccount_sid: twilioSid.trim(),
      retell_agent_ids: agentIds,
      phone_numbers: phoneNumbers,
    }
    try {
      if (preview) {
        savePreviewMapping(locationId, fields)
        onSaved({ location_id: locationId, client_name: clientName, ...fields })
      } else {
        onSaved(await saveMapping(locationId, fields))
      }
    } catch (err) {
      // The backend's own validation message is not carried by ApiError (it only keeps the
      // status), so a 400 gets one plain sentence naming where to look rather than its wording.
      setError(
        err instanceof ApiError && err.status === 400
          ? 'Check the Twilio subaccount SID and the phone numbers below.'
          : 'Could not save. Try again.',
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mapping-editor">
      <label className="mapping-field">
        <span className="tile-label">Twilio subaccount SID</span>
        <input
          type="text"
          value={twilioSid}
          placeholder="AC…"
          onChange={(event) => setTwilioSid(event.target.value)}
        />
      </label>

      <ListEditor label="Retell agent IDs" items={agentIds} onChange={setAgentIds} placeholder="agent_…" />
      <ListEditor
        label="Phone numbers"
        items={phoneNumbers}
        onChange={setPhoneNumbers}
        placeholder="+1…"
      />

      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}

      <button type="button" className="button" onClick={handleSave} disabled={saving}>
        {saving ? 'Saving…' : 'Save mapping'}
      </button>
    </div>
  )
}

/** A short list of typed-in values - agent IDs, phone numbers - each removable, with one input
 * to add the next. Enter adds without needing the mouse. */
function ListEditor({
  label,
  items,
  onChange,
  placeholder,
}: {
  label: string
  items: string[]
  onChange: (items: string[]) => void
  placeholder: string
}) {
  const [draft, setDraft] = useState('')

  function add() {
    const value = draft.trim()
    if (value && !items.includes(value)) onChange([...items, value])
    setDraft('')
  }

  return (
    <div className="mapping-field">
      <span className="tile-label">{label}</span>
      <ul className="chip-list">
        {items.map((item) => (
          <li key={item} className="chip">
            <code>{item}</code>
            <button
              type="button"
              aria-label={`Remove ${item}`}
              onClick={() => onChange(items.filter((candidate) => candidate !== item))}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      <div className="chip-add">
        <input
          type="text"
          value={draft}
          placeholder={placeholder}
          aria-label={`Add a ${label.toLowerCase().replace(/s$/, '')}`}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              add()
            }
          }}
        />
        <button type="button" className="button ghost" onClick={add}>
          Add
        </button>
      </div>
    </div>
  )
}
