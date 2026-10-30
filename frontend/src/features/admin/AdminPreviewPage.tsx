import { useState } from 'react'
import type { ClientDetailResult } from '../../types/admin'
import './admin.css'
import ClientBrowser, { Shell } from './ClientBrowser'
import { DEMO_SESSION, demoClients, demoDetail } from './demoData'
import { currentMonth } from './format'

/**
 * The standalone demo walkthrough, on its own route so it never competes with `/admin`.
 *
 * Bundled sample data (`demoData.ts`), never the admin API: whoever reaches this route sees
 * invented clients and figures, never a real one's. Useful for a walkthrough before the domain
 * and the Custom Page exist, or any time someone wants to see the page without a GHL session.
 */
export default function AdminPreviewPage() {
  const [month, setMonth] = useState(currentMonth())
  const [selected, setSelected] = useState<string | null>(null)
  // Forces a re-render after a preview mapping edit, which otherwise only changes data held
  // outside React state (see demoData's `previewMappings`).
  const [, bumpPreview] = useState(0)

  const shown = demoClients(month)
  const openDetail: ClientDetailResult | null = selected ? demoDetail(selected, month) : null

  return (
    <Shell userName={DEMO_SESSION.user_name}>
      <ClientBrowser
        banner={
          <div className="banner warn" role="status">
            <strong>Standalone preview.</strong> These are sample figures, not live billing data.
            This page never calls the real API.
          </div>
        }
        month={month}
        onMonthChange={setMonth}
        selected={selected}
        onSelect={setSelected}
        onBack={() => setSelected(null)}
        shown={shown}
        openDetail={openDetail}
        preview
        onMappingSaved={() => bumpPreview((tick) => tick + 1)}
        // The sample detail is recomputed on every render, so a preview save only needs the
        // page to render again - `demoData` already holds what was saved.
        onRatesSaved={() => bumpPreview((tick) => tick + 1)}
      />
    </Shell>
  )
}
