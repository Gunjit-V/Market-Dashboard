import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Instruments from './pages/Instruments'
import OHLCV from './pages/OHLCV'
import Download from './pages/Download'

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/instruments" element={<Instruments />} />
        <Route path="/ohlcv/:symbol" element={<OHLCV />} />
        <Route path="/download" element={<Download />} />
      </Routes>
    </Layout>
  )
}

export default App
