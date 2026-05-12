import { useState, useEffect, useCallback } from 'react'
import apiBase from '../apiBase.js'
import './SystemPanel.css'

/**
 * SystemPanel — Live system-state sidebar.
 *
 * Polls GET /system-state every VITE_POLL_INTERVAL_MS milliseconds (default
 * 5 000 ms) and renders CPU, memory, disk, active window, running apps, and
 * recent files.  Also displays whether Ollama is reachable via GET /health.
 *
 * All polling is self-contained — no props required.
 */
export default function SystemPanel() {
  const [state,       setState]       = useState(null)
  const [ollamaOk,    setOllamaOk]    = useState(null)   // null = unknown
  const [error,       setError]       = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)

  const pollInterval = parseInt(
    import.meta.env.VITE_POLL_INTERVAL_MS || '5000', 10
  )

  /** Fetch system state from the backend. */
  const fetchState = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase}/system-state`)
      const data     = await response.json()

      if (!response.ok || data.success === false) {
        setError(data.detail || data.error || `HTTP ${response.status}`)
      } else {
        setState(data)
        setError(null)
        setLastUpdated(new Date())
      }
    } catch (err) {
      setError(err.message || 'Network error')
    }
  }, [apiBase])

  /** Fetch health (Ollama reachability) from the backend. */
  const fetchHealth = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase}/health`)
      if (!response.ok) return
      const data = await response.json()
      setOllamaOk(data.ollama?.reachable ?? null)
    } catch {
      setOllamaOk(false)
    }
  }, [apiBase])

  // Initial fetch + polling
  useEffect(() => {
    fetchState()
    fetchHealth()
    const stateTimer  = setInterval(fetchState,  pollInterval)
    const healthTimer = setInterval(fetchHealth, pollInterval)
    return () => {
      clearInterval(stateTimer)
      clearInterval(healthTimer)
    }
  }, [fetchState, fetchHealth, pollInterval])

  const updatedText = lastUpdated
    ? lastUpdated.toLocaleTimeString([], {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      })
    : null

  return (
    <div className="system-panel">
      {/* Header */}
      <div className="system-panel__header">
        <h2 className="system-panel__title">System</h2>
        {updatedText && (
          <span className="system-panel__updated">{updatedText}</span>
        )}
      </div>

      {/* Ollama status pill */}
      {ollamaOk !== null && (
        <div className={`ollama-pill ollama-pill--${ollamaOk ? 'ok' : 'down'}`}>
          <span className="ollama-pill__dot" aria-hidden="true" />
          {ollamaOk ? 'Ollama connected' : 'Ollama unreachable'}
        </div>
      )}

      {/* Error state */}
      {error && (
        <div className="system-panel__error" role="alert">
          {error}
        </div>
      )}

      {/* Loading state */}
      {!state && !error && (
        <div className="system-panel__loading">
          <span className="spinner" />
          Loading&hellip;
        </div>
      )}

      {/* Data */}
      {state && (
        <>
          {state.active_window && (
            <Section title="Active Window">
              <p className="system-panel__window" title={state.active_window}>
                {state.active_window}
              </p>
            </Section>
          )}

          {state.cpu_percent !== undefined && (
            <Section title="CPU">
              <GaugeBar
                value={state.cpu_percent}
                label={`${state.cpu_percent.toFixed(1)}\u202f%`}
                thresholds={{ warn: 60, critical: 85 }}
              />
            </Section>
          )}

          {state.memory && (
            <Section title="Memory">
              <GaugeBar
                value={state.memory.percent}
                label={`${state.memory.used_gb?.toFixed(1)}\u202f/\u202f${state.memory.total_gb?.toFixed(1)}\u202fGB`}
                thresholds={{ warn: 70, critical: 90 }}
              />
            </Section>
          )}

          {state.disk && (
            <Section title={`Disk (${state.disk.path ?? 'C:/'})`}>
              <GaugeBar
                value={state.disk.percent}
                label={`${state.disk.used_gb?.toFixed(0)}\u202f/\u202f${state.disk.total_gb?.toFixed(0)}\u202fGB`}
                thresholds={{ warn: 75, critical: 90 }}
              />
            </Section>
          )}

          {Array.isArray(state.running_apps) && state.running_apps.length > 0 && (
            <Section title={`Running (${state.running_apps.length})`}>
              <ul className="system-panel__chips" aria-label="Running applications">
                {state.running_apps.map((a) => (
                  <li key={a} className="system-panel__chip">{a}</li>
                ))}
              </ul>
            </Section>
          )}

          {Array.isArray(state.recent_files) && state.recent_files.length > 0 && (
            <Section title="Recent Files">
              <ul className="system-panel__files" aria-label="Recent files">
                {state.recent_files.map((f) => (
                  <li key={f} className="system-panel__file" title={f}>{f}</li>
                ))}
              </ul>
            </Section>
          )}
        </>
      )}
    </div>
  )
}

// ── Sub-components ────────────────────────────────────────────────────────

/**
 * Section — labelled group within the panel.
 * @param {{ title: string, children: React.ReactNode }} props
 */
function Section({ title, children }) {
  return (
    <div className="system-panel__section">
      <h3 className="system-panel__section-title">{title}</h3>
      {children}
    </div>
  )
}

/**
 * GaugeBar — colour-coded progress bar.
 * @param {{ value: number, label: string, thresholds: {warn: number, critical: number} }} props
 */
function GaugeBar({ value, label, thresholds }) {
  const pct = Math.max(0, Math.min(100, value))
  let colour = 'ok'
  if (pct >= thresholds.critical) colour = 'critical'
  else if (pct >= thresholds.warn) colour = 'warn'

  return (
    <div className="gauge">
      <div className="gauge__track">
        <div
          className={`gauge__fill gauge__fill--${colour}`}
          style={{ width: `${pct}%` }}
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
        />
      </div>
      <span className="gauge__label">{label}</span>
    </div>
  )
}
