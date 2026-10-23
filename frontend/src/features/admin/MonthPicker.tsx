import { formatMonth, recentMonths, shiftMonth } from './format'

interface Props {
  month: string
  onChange: (month: string) => void
  /** True while the selected period's figures are still loading. */
  busy?: boolean
}

/**
 * Which billing month the page is showing.
 *
 * The arrows are for stepping one month at a time, which is what the month-on-month question
 * usually is; the list is for jumping. The range is bounded at both ends — there is no usage in
 * the future, and the arrows stop at the oldest listed month so the select can never hold a
 * value that is not one of its options.
 */
export default function MonthPicker({ month, onChange, busy = false }: Props) {
  const months = recentMonths()
  const newest = months[0]
  const oldest = months[months.length - 1]

  return (
    <div className="month-picker">
      <button
        type="button"
        className="step"
        aria-label="Previous month"
        onClick={() => onChange(shiftMonth(month, -1))}
        disabled={busy || month <= oldest}
      >
        &lsaquo;
      </button>

      <select
        className="month-select"
        aria-label="Billing month"
        value={month}
        onChange={(event) => onChange(event.target.value)}
        disabled={busy}
      >
        {months.map((option) => (
          <option key={option} value={option}>
            {formatMonth(option)}
          </option>
        ))}
      </select>

      <button
        type="button"
        className="step"
        aria-label="Next month"
        onClick={() => onChange(shiftMonth(month, 1))}
        disabled={busy || month >= newest}
      >
        &rsaquo;
      </button>
    </div>
  )
}
