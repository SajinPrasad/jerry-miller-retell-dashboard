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

/** "2026-03" -> "March 2026". */
export function formatMonth(month: string): string {
  const [year, number] = month.split('-')
  const names = [
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
  return `${names[Number(number) - 1] ?? month} ${year}`
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
