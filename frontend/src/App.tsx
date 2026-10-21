import { Navigate, Route, Routes } from 'react-router-dom'
import OnboardingPage from './features/onboarding/OnboardingPage'

export default function App() {
  return (
    <Routes>
      <Route path="/onboarding" element={<OnboardingPage />} />
      <Route path="*" element={<Navigate to="/onboarding" replace />} />
    </Routes>
  )
}
