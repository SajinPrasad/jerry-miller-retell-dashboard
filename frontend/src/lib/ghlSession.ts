/**
 * The handshake that gets this page its GoHighLevel session.
 *
 * The admin page is a GHL Custom Page: it runs in an iframe and asks the parent window for the
 * session, which comes back as an encrypted blob only our backend can open. The blob is the
 * page's credential, so it is kept in memory and sent on every admin request.
 *
 * Docs: https://marketplace.gohighlevel.com/docs/other/user-context-marketplace-apps
 */

const REQUEST = 'REQUEST_USER_DATA'
const RESPONSE = 'REQUEST_USER_DATA_RESPONSE'

// GHL normally answers at once. Past this, the page is almost certainly not inside GHL.
const TIMEOUT_MS = 10_000

// In memory only, for the life of the page. Not localStorage: it is a credential, and asking
// again costs one postMessage.
let sessionBlob: string | null = null

export function getSessionBlob(): string | null {
  return sessionBlob
}

export class NoSessionError extends Error {}

export function requestSessionBlob(): Promise<string> {
  // Outside GHL there is no parent to answer. A blob captured once from the browser can be put
  // in VITE_DEV_SSO_BLOB to work on the page locally; the backend still verifies it exactly as
  // it verifies a real one, so this is a way in, not a way around.
  const devBlob = import.meta.env.VITE_DEV_SSO_BLOB
  if (devBlob) {
    sessionBlob = devBlob
    return Promise.resolve(devBlob)
  }

  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      stop()
      reject(new NoSessionError('GoHighLevel did not answer the session request.'))
    }, TIMEOUT_MS)

    function stop() {
      window.clearTimeout(timer)
      window.removeEventListener('message', onMessage)
    }

    function onMessage(event: MessageEvent) {
      if (event.data?.message !== RESPONSE) return
      stop()
      const payload = event.data.payload
      // Treated as opaque: nothing here reads it, because only the backend can tell whether it
      // is genuine. The origin is not pinned either — a white-labelled parent has no fixed
      // origin, and a blob from a hostile parent is one it already had.
      if (typeof payload === 'string' && payload) {
        sessionBlob = payload
        resolve(payload)
      } else {
        reject(new NoSessionError('GoHighLevel returned an empty session.'))
      }
    }

    window.addEventListener('message', onMessage)
    window.parent.postMessage({ message: REQUEST }, '*')
  })
}
