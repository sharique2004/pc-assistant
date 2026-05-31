import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import BibiApp from './bibi/BibiApp.jsx'

document.documentElement.dataset.bibi = '1'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BibiApp />
  </StrictMode>,
)
