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

// GHL normally answers at once. Past this, whatever is framing us is not going to reply.
const TIMEOUT_MS = 10_000

// In memory only, for the life of the page. Not localStorage: it is a credential, and asking
// again costs one postMessage.
let sessionBlob: string | null = null

export function getSessionBlob(): string | null {
  return sessionBlob
}

/**
 * The GHL session blob, or null when there is no GoHighLevel to ask.
 *
 * Null is an ordinary outcome, not a failure: the page is opened directly in a browser while
 * the domain and the Custom Page are still being set up. The caller decides what to do about
 * it — see `AdminPage`, which falls back to a clearly-marked sample preview.
 */
export async function requestSessionBlob(): Promise<string | null> {
  // A blob captured once from the browser can be put in VITE_DEV_SSO_BLOB to work on the page
  // locally. The backend still verifies it exactly as it verifies a real one, so this is a way
  // in, not a way around.
  const devBlob = import.meta.env.VITE_DEV_SSO_BLOB
  if (devBlob) {
    sessionBlob = devBlob
    return devBlob
  }

  // Not framed at all, so there is no parent to answer and nothing to wait for. Checked first
  // so direct access falls through immediately instead of sitting on the timeout.
  if (window.parent === window) return null

  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      stop()
      resolve(null)
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
        resolve(null)
      }
    }

    window.addEventListener('message', onMessage)
    window.parent.postMessage({ message: REQUEST }, '*')
  })
}
