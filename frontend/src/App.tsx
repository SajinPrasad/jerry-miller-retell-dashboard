import { Navigate, Route, Routes } from 'react-router-dom'
import AdminPage from './features/admin/AdminPage'
import OnboardingPage from './features/onboarding/OnboardingPage'

export default function App() {
  return (
    <Routes>
      {/* The GHL Custom Page points here. */}
      <Route path="/admin" element={<AdminPage />} />
      <Route path="/onboarding" element={<OnboardingPage />} />
      <Route path="*" element={<Navigate to="/onboarding" replace />} />
    </Routes>
  )
}
