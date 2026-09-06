import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Instruments from './pages/Instruments'
import OHLCV from './pages/OHLCV'
import Download from './pages/Download'
import Volatility from './pages/Volatility'
import Strategies from './pages/Strategies'

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/instruments" element={<Instruments />} />
        <Route path="/ohlcv/:symbol" element={<OHLCV />} />
        <Route path="/download" element={<Download />} />
        <Route path="/volatility" element={<Volatility />} />
        <Route path="/strategies" element={<Strategies />} />
      </Routes>
    </Layout>
  )
}

export default App
