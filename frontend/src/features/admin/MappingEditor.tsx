import { useState } from 'react'
import { getRetellAgents, getTwilioSubaccounts, saveMapping } from '../../api/admin'
import { ApiError } from '../../api/client'
import type { MappingFields, MappingResult, RetellAgent } from '../../types/admin'
import SearchableSelect, { type Option } from './SearchableSelect'
import { demoRetellAgents, demoTwilioSubaccounts, savePreviewMapping } from './demoData'

interface Props {
  locationId: string
  clientName: string
  initial: MappingFields
  /** The names behind `initial.retell_agent_ids`, in the same order - what the chip list reads
   * before the picker has been opened once to learn any more. */
  agentNames: RetellAgent[]
  /** False while Twilio rebilling is paused: the subaccount picker and the phone numbers are
   * hidden, and a save leaves both fields out so the stored ones are preserved. */
  twilioEnabled: boolean
  /** The standalone preview never reaches the admin API: saving here only updates the sample
   * data held for this tab, and the pickers read sample agents and subaccounts too. */
  preview: boolean
  onSaved: (result: MappingResult) => void
}

/**
 * The one thing an agency admin can write on a client's page: their Retell agents, their backup
 * phone numbers, and their Twilio subaccount. Rates and billing mode have their own rules and
 * are not edited here, and neither is the Stripe customer - that is matched automatically from
 * the sub-account owner's email, and `ClientDetail` shows the result read-only.
 *
 * The agent and the subaccount are chosen from what Retell and Twilio actually have, not typed:
 * an id that came out of a picker cannot be a typo, and a mapping nobody can see is a client
 * whose minutes quietly land in `UnassignedCall`. Phone numbers stay typed - they are the backup
 * match for calls an agent id does not cover, and they come off a GHL sub-account, not Retell.
 *
 * While Twilio rebilling is paused (`twilioEnabled` false) only the Retell agents are editable:
 * the subaccount picker and the phone numbers are hidden rather than removed, and a save leaves
 * their two fields out of the request so the stored values are left exactly as they are.
 */
export default function MappingEditor({
  locationId,
  clientName,
  initial,
  agentNames,
  twilioEnabled,
  preview,
  onSaved,
}: Props) {
  const [twilioSid, setTwilioSid] = useState(initial.twilio_subaccount_sid)
  const [agentIds, setAgentIds] = useState(initial.retell_agent_ids)
  const [phoneNumbers, setPhoneNumbers] = useState(initial.phone_numbers)
  // What each agent id on this client is called, for the chip list. Seeded from the page's own
  // read, and filled in as the picker is used - the picker already knows the name of whatever
  // it just offered, so asking the backend again for it would be asking for what is on screen.
  const [agentLabels, setAgentLabels] = useState<Record<string, string>>(() =>
    Object.fromEntries(agentNames.map((agent) => [agent.agent_id, agent.agent_name])),
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Scoped to this client: the backend (and the preview's copy of it) leaves out any agent
  // already mapped to another one. Fetched on the picker's first open, and the editor is
  // remounted per client, so a client opened after an agent was freed elsewhere sees it back.
  async function loadAgents(): Promise<Option[]> {
    const agents = preview ? demoRetellAgents(locationId) : await getRetellAgents(locationId)
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
        // Only what is editable right now goes to the backend. `set_mapping()` writes only the
        // fields it is sent, so omitting the two hidden ones keeps whatever is stored.
        onSaved(
          await saveMapping(
            locationId,
            twilioEnabled ? fields : { retell_agent_ids: agentIds },
          ),
        )
      }
    } catch (err) {
      // The backend's own validation message is not carried by ApiError (it only keeps the
      // status), so a 400 gets one plain sentence naming where to look rather than its wording.
      setError(
        err instanceof ApiError && err.status === 400 && twilioEnabled
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
          labels={agentLabels}
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
          onChoose={(option) => {
            setAgentIds([...agentIds, option.value])
            setAgentLabels({ ...agentLabels, [option.value]: option.label })
          }}
        />
      </div>

      {twilioEnabled && (
        <>
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
        </>
      )}

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

/**
 * What is mapped so far, each removable.
 *
 * Phone numbers have no name behind them and are left as `<code>` - an id with a known label
 * (an agent) shows the label instead, falling back to the id for one `labels` has nothing for,
 * the same way the backend's own fallback does.
 */
function ChipList({
  items,
  labels,
  onRemove,
  empty,
}: {
  items: string[]
  labels?: Record<string, string>
  onRemove: (item: string) => void
  empty: string
}) {
  if (items.length === 0) return <p className="muted small">{empty}</p>
  return (
    <ul className="chip-list">
      {items.map((item) => {
        const label = labels?.[item]
        return (
          <li key={item} className="chip">
            {label ? <span>{label}</span> : <code>{item}</code>}
            <button
              type="button"
              aria-label={`Remove ${label || item}`}
              onClick={() => onRemove(item)}
            >
              ×
            </button>
          </li>
        )
      })}
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
