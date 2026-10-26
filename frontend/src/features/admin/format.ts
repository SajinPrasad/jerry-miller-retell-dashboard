/** Display helpers. Money arrives as integer cents and is formatted without ever becoming a float. */

export const DASH = '—'

export function formatCents(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return DASH
  const sign = cents < 0 ? '-' : ''
  const abs = Math.abs(cents)
  return `${sign}$${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, '0')}`
}

/** A per-minute rate, e.g. 25 -> "$0.25". */
export function formatRate(cents: number | null | undefined): string {
  return formatCents(cents)
}

export function formatMinutes(minutes: string | null | undefined): string {
  return minutes === null || minutes === undefined ? DASH : minutes
}

/** The date part of an ISO timestamp. Stable wherever it renders, unlike a locale format. */
export function formatDate(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : DASH
}

const MONTH_NAMES = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

/** "2026-03" -> "March 2026". */
export function formatMonth(month: string): string {
  const [year, number] = month.split('-')
  return `${MONTH_NAMES[Number(number) - 1] ?? month} ${year}`
}

/**
 * An ISO timestamp as a time someone can read: "2026-10-03T14:45:00Z" -> "Oct 3, 2:45 PM".
 *
 * Read off the string rather than through `Date`, so the time shown is the UTC one the call was
 * recorded at. The billing window, the Twilio days and the period filter are all UTC; a call
 * re-stamped into the viewer's timezone could sit under a date in a different month from the
 * one it is billed in. The year is left off: the period filter already says which it is.
 */
export function formatDateTime(iso: string | null | undefined): string {
  const parts = iso ? /^\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso) : null
  if (!parts) return DASH

  const [, month, day, hour, minute] = parts
  const hours = Number(hour)
  const clock = hours % 12 === 0 ? 12 : hours % 12
  return `${MONTH_NAMES[Number(month) - 1]?.slice(0, 3)} ${Number(day)}, ${clock}:${minute} ${
    hours < 12 ? 'AM' : 'PM'
  }`
}

/** A dialled number as it is written: "+12137771234" -> "(213) 777-1234". Anything else as-is. */
export function formatPhone(number: string | null | undefined): string {
  if (!number) return DASH
  const parts = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(number)
  return parts ? `(${parts[1]}) ${parts[2]}-${parts[3]}` : number
}

/** Cents as the plain decimal a money field holds, e.g. 25 -> "0.25". */
export function centsToInput(cents: number): string {
  return (cents / 100).toFixed(2)
}

/** What a money field holds, as whole cents. Anything unreadable, or negative, counts as zero. */
export function centsFromInput(value: string): number {
  const amount = Number.parseFloat(value)
  return Number.isFinite(amount) && amount > 0 ? Math.round(amount * 100) : 0
}

/**
 * What Jerry keeps: the estimate minus what Retell and Twilio charge him. Null when there is
 * no rate, because the estimate it comes from is null too.
 */
export function marginCents(
  estimated: number | null,
  aiCost: number,
  callCost: number,
): number | null {
  return estimated === null ? null : estimated - aiCost - callCost
}


/**
 * The current billing month, in UTC.
 *
 * UTC because that is what the backend defaults to (`TIME_ZONE` is UTC and Twilio's days are
 * GMT), so the picker and the server agree about which month "this month" is even when the
 * viewer's local date has already rolled over.
 */
export function currentMonth(): string {
  const now = new Date()
  return asMonth(now.getUTCFullYear(), now.getUTCMonth() + 1)
}

/** `month` moved by `delta` months, e.g. shiftMonth('2026-01', -1) -> '2025-12'. */
export function shiftMonth(month: string, delta: number): string {
  const [year, number] = month.split('-').map(Number)
  const moved = new Date(Date.UTC(year, number - 1 + delta, 1))
  return asMonth(moved.getUTCFullYear(), moved.getUTCMonth() + 1)
}

/** The selectable billing months, newest first. Never the future: nothing is billed there. */
export function recentMonths(count = 12, from: string = currentMonth()): string[] {
  return Array.from({ length: count }, (_, index) => shiftMonth(from, -index))
}

function asMonth(year: number, number: number): string {
  return `${year}-${String(number).padStart(2, '0')}`
}
