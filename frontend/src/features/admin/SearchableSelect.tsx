import { useEffect, useRef, useState } from 'react'

export interface Option {
  /** What gets stored - an agent id, a subaccount SID, or '' for "none". */
  value: string
  label: string
  /** The id behind the name, so two agents called the same thing are still tellable apart. */
  detail?: string
}

interface Props {
  /** What the closed trigger reads: the current choice, or an invitation to make one. */
  triggerLabel: string
  /** Names the search box, and stands in as its placeholder. */
  searchLabel: string
  /** Shown in place of the list when the provider could not be reached. */
  errorMessage: string
  /** Run the first time the list is opened, and again on the next open after a failure. */
  load: () => Promise<Option[]>
  /** Values already chosen: marked in the list, and not selectable twice. */
  chosen: string[]
  chosenLabel: string
  onChoose: (option: Option) => void
}

/**
 * A text field replaced by the list it was always meant to hold: the Retell agents and Twilio
 * subaccounts that really exist, searchable, instead of an id copied across from another tab.
 *
 * The list is fetched when the panel is first opened rather than when the page loads, because
 * most visits to a client's page never touch the mapping - and each one costs a live call to
 * Retell or Twilio.
 */
export default function SearchableSelect({
  triggerLabel,
  searchLabel,
  errorMessage,
  load,
  chosen,
  chosenLabel,
  onChoose,
}: Props) {
  const [open, setOpen] = useState(false)
  const [options, setOptions] = useState<Option[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)
  const [query, setQuery] = useState('')
  const root = useRef<HTMLDivElement>(null)

  // Closing anywhere else on the page, the way a dropdown is expected to behave. Only listened
  // for while something is open, so a page full of closed pickers costs nothing.
  useEffect(() => {
    if (!open) return
    function onPointerDown(event: PointerEvent | MouseEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [open])

  function toggle() {
    if (open) {
      setOpen(false)
      return
    }
    setOpen(true)
    setQuery('')
    // Nothing loaded, or the last attempt failed: ask again. A list we already have is reused,
    // so reopening the panel to search it is free.
    if (options === null && !loading) {
      setLoading(true)
      setFailed(false)
      load()
        .then(setOptions)
        .catch(() => setFailed(true))
        .finally(() => setLoading(false))
    }
  }

  function choose(option: Option) {
    onChoose(option)
    setOpen(false)
  }

  const wanted = query.trim().toLowerCase()
  const matches = (options ?? []).filter(
    (option) =>
      !wanted ||
      option.label.toLowerCase().includes(wanted) ||
      option.value.toLowerCase().includes(wanted),
  )

  return (
    <div className="select" ref={root}>
      <button
        type="button"
        className="select-trigger"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={toggle}
      >
        <span className="select-trigger-label">{triggerLabel}</span>
        <span aria-hidden="true">▾</span>
      </button>

      {open && (
        <div className="select-panel" onKeyDown={(event) => event.key === 'Escape' && setOpen(false)}>
          <input
            type="text"
            className="select-search"
            value={query}
            aria-label={searchLabel}
            placeholder={searchLabel}
            autoFocus
            onChange={(event) => setQuery(event.target.value)}
          />

          {loading && <p className="select-note muted small">Loading…</p>}
          {failed && <p className="select-note muted small">{errorMessage}</p>}

          {!loading && !failed && options !== null && (
            matches.length > 0 ? (
              <ul className="select-options">
                {matches.map((option) => {
                  const taken = chosen.includes(option.value)
                  return (
                    <li key={option.value}>
                      <button
                        type="button"
                        className="select-option"
                        disabled={taken}
                        onClick={() => choose(option)}
                      >
                        <span className="select-option-label">{option.label}</span>
                        {option.detail && <code>{option.detail}</code>}
                        {taken && <span className="pill ok">{chosenLabel}</span>}
                      </button>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <p className="select-note muted small">Nothing matches that search.</p>
            )
          )}
        </div>
      )}
    </div>
  )
}
