import { Navigate, Route, Routes } from 'react-router-dom'
import AdminPage from './features/admin/AdminPage'
import AdminPreviewPage from './features/admin/AdminPreviewPage'
import OnboardingPage from './features/onboarding/OnboardingPage'

export default function App() {
  return (
    <Routes>
      {/* The GHL Custom Page points here. Always real data - see /admin/preview for the demo. */}
      <Route path="/admin" element={<AdminPage />} />
      <Route path="/admin/preview" element={<AdminPreviewPage />} />
      <Route path="/onboarding" element={<OnboardingPage />} />
      <Route path="*" element={<Navigate to="/onboarding" replace />} />
    </Routes>
  )
}
