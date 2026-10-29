import { useState } from 'react'
import { getRetellAgents, getTwilioSubaccounts, saveMapping } from '../../api/admin'
import { ApiError } from '../../api/client'
import type { MappingFields, MappingResult } from '../../types/admin'
import SearchableSelect, { type Option } from './SearchableSelect'
import { demoRetellAgents, demoTwilioSubaccounts, savePreviewMapping } from './demoData'

interface Props {
  locationId: string
  clientName: string
  initial: MappingFields
  /** The standalone preview never reaches the admin API: saving here only updates the sample
   * data held for this tab, and the pickers read sample agents and subaccounts too. */
  preview: boolean
  onSaved: (result: MappingResult) => void
}

/**
 * The one thing an agency admin can write on a client's page: their Retell agents, their backup
 * phone numbers, and their Twilio subaccount. Rates and billing mode have their own rules and
 * are not edited here.
 *
 * The agent and the subaccount are chosen from what Retell and Twilio actually have, not typed:
 * an id that came out of a picker cannot be a typo, and a mapping nobody can see is a client
 * whose minutes quietly land in `UnassignedCall`. Phone numbers stay typed - they are the backup
 * match for calls an agent id does not cover, and they come off a GHL sub-account, not Retell.
 */
export default function MappingEditor({ locationId, clientName, initial, preview, onSaved }: Props) {
  const [twilioSid, setTwilioSid] = useState(initial.twilio_subaccount_sid)
  const [agentIds, setAgentIds] = useState(initial.retell_agent_ids)
  const [phoneNumbers, setPhoneNumbers] = useState(initial.phone_numbers)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function loadAgents(): Promise<Option[]> {
    const agents = preview ? demoRetellAgents() : await getRetellAgents()
    return agents.map((agent) => ({
      value: agent.agent_id,
      label: agent.agent_name || agent.agent_id,
      detail: agent.agent_id,
    }))
  }

  async function loadSubaccounts(): Promise<Option[]> {
    const subaccounts = preview ? demoTwilioSubaccounts() : await getTwilioSubaccounts()
    return [
      // Clearing the subaccount is a real choice: it stops this client being billed for regular
      // calls, the same as an empty field used to.
      { value: '', label: 'No Twilio subaccount' },
      ...subaccounts.map((subaccount) => ({
        value: subaccount.sid,
        label: subaccount.friendly_name || subaccount.sid,
        detail: subaccount.sid,
      })),
    ]
  }

  async function handleSave() {
    setSaving(true)
    setError(null)
    const fields: MappingFields = {
      twilio_subaccount_sid: twilioSid,
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
          ? 'Check the phone numbers below.'
          : 'Could not save. Try again.',
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mapping-editor">
      <div className="mapping-field">
        <span className="tile-label">Retell agents</span>
        <ChipList
          items={agentIds}
          onRemove={(agentId) => setAgentIds(agentIds.filter((item) => item !== agentId))}
          empty="No agents yet."
        />
        <SearchableSelect
          triggerLabel="Add a Retell agent"
          searchLabel="Search Retell agents"
          errorMessage="Could not load the Retell agents. Close this and try again."
          load={loadAgents}
          chosen={agentIds}
          chosenLabel="Added"
          onChoose={(option) => setAgentIds([...agentIds, option.value])}
        />
      </div>

      <div className="mapping-field">
        <span className="tile-label">Twilio subaccount</span>
        <SearchableSelect
          triggerLabel={twilioSid || 'Choose a Twilio subaccount'}
          searchLabel="Search Twilio subaccounts"
          errorMessage="Could not load the Twilio subaccounts. Close this and try again."
          load={loadSubaccounts}
          chosen={[twilioSid]}
          chosenLabel="Selected"
          onChoose={(option) => setTwilioSid(option.value)}
        />
      </div>

      <PhoneNumbers numbers={phoneNumbers} onChange={setPhoneNumbers} />

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

/** What is mapped so far, each removable. */
function ChipList({
  items,
  onRemove,
  empty,
}: {
  items: string[]
  onRemove: (item: string) => void
  empty: string
}) {
  if (items.length === 0) return <p className="muted small">{empty}</p>
  return (
    <ul className="chip-list">
      {items.map((item) => (
        <li key={item} className="chip">
          <code>{item}</code>
          <button type="button" aria-label={`Remove ${item}`} onClick={() => onRemove(item)}>
            ×
          </button>
        </li>
      ))}
    </ul>
  )
}

/** The client's own lines, typed in: the backup match for a call no mapped agent covers. */
function PhoneNumbers({
  numbers,
  onChange,
}: {
  numbers: string[]
  onChange: (numbers: string[]) => void
}) {
  const [draft, setDraft] = useState('')

  function add() {
    const value = draft.trim()
    if (value && !numbers.includes(value)) onChange([...numbers, value])
    setDraft('')
  }

  return (
    <div className="mapping-field">
      <span className="tile-label">Phone numbers</span>
      <ChipList
        items={numbers}
        onRemove={(number) => onChange(numbers.filter((item) => item !== number))}
        empty="No phone numbers yet."
      />
      <div className="chip-add">
        <input
          type="text"
          value={draft}
          placeholder="+1…"
          aria-label="Add a phone number"
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
