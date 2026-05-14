import { useState, useCallback, useEffect, useRef } from 'react'
import CommandBar from './components/CommandBar.jsx'
import StatusFeed from './components/StatusFeed.jsx'
import SystemPanel from './components/SystemPanel.jsx'
import ConfirmModal from './components/ConfirmModal.jsx'
import VoicePanel from './components/VoicePanel.jsx'
import apiBase from './apiBase.js'
import './App.css'

const VOICE_ENABLED_KEY = 'pc-assistant.voice-enabled'
const VOICE_URI_KEY = 'pc-assistant.voice-uri'
const VOICE_RATE_KEY = 'pc-assistant.voice-rate'
const CONTINUOUS_MODE_KEY = 'pc-assistant.continuous-mode'
const DARK_MODE_KEY = 'pc-assistant.dark-mode'
const CONTINUOUS_REARM_MS = 950

export default function App() {
  const [commands, setCommands] = useState([])
  const [pendingConfirm, setPendingConfirm] = useState(null)
  const [voices, setVoices] = useState([])
  const [isSpeaking, setIsSpeaking] = useState(false)
  const [voiceEnabled, setVoiceEnabled] = useState(() => readStoredBoolean(VOICE_ENABLED_KEY, true))
  const [selectedVoiceURI, setSelectedVoiceURI] = useState(() => readStoredString(VOICE_URI_KEY, 'windows-default'))
  const [speechRate, setSpeechRate] = useState(() => readStoredNumber(VOICE_RATE_KEY, 1.0))
  const [isContinuous, setIsContinuous] = useState(() => readStoredBoolean(CONTINUOUS_MODE_KEY, false))
  const [isDarkMode, setIsDarkMode] = useState(() => readStoredBoolean(DARK_MODE_KEY, false))
  const [autoListenSignal, setAutoListenSignal] = useState(0)

  const currentAudioRef = useRef(null)
  const currentAudioUrlRef = useRef('')
  const continuousArmTimerRef = useRef(null)
  const latestContinuousRef = useRef(isContinuous)
  const latestPendingConfirmRef = useRef(pendingConfirm)

  const speechSupported = typeof window !== 'undefined' && typeof window.Audio !== 'undefined'
  const latestCommand = commands.length ? commands[commands.length - 1] : null
  const heroState = buildHeroState({
    latestCommand,
    pendingConfirm,
    isSpeaking,
    voiceEnabled,
    isContinuous,
  })

  useEffect(() => {
    latestContinuousRef.current = isContinuous
  }, [isContinuous])

  useEffect(() => {
    latestPendingConfirmRef.current = pendingConfirm
    if (pendingConfirm) {
      window.clearTimeout(continuousArmTimerRef.current)
    }
  }, [pendingConfirm])

  useEffect(() => {
    setVoices([{ voiceURI: 'windows-default', name: 'Windows Default', lang: 'en-US', localService: true }])
    setSelectedVoiceURI((current) => current || 'windows-default')
  }, [])

  useEffect(() => {
    writeStoredBoolean(VOICE_ENABLED_KEY, voiceEnabled)
  }, [voiceEnabled])

  useEffect(() => {
    writeStoredString(VOICE_URI_KEY, selectedVoiceURI)
  }, [selectedVoiceURI])

  useEffect(() => {
    writeStoredString(VOICE_RATE_KEY, String(speechRate))
  }, [speechRate])

  useEffect(() => {
    writeStoredBoolean(CONTINUOUS_MODE_KEY, isContinuous)
  }, [isContinuous])

  useEffect(() => {
    if (typeof document === 'undefined') {
      return
    }

    document.documentElement.dataset.theme = isDarkMode ? 'dark' : 'light'
    writeStoredBoolean(DARK_MODE_KEY, isDarkMode)
  }, [isDarkMode])

  const releaseCurrentAudio = useCallback(() => {
    if (currentAudioRef.current) {
      currentAudioRef.current.onended = null
      currentAudioRef.current.onerror = null
      currentAudioRef.current.pause()
      currentAudioRef.current = null
    }

    if (currentAudioUrlRef.current) {
      URL.revokeObjectURL(currentAudioUrlRef.current)
      currentAudioUrlRef.current = ''
    }
  }, [])

  const armContinuousListening = useCallback((delayMs = CONTINUOUS_REARM_MS) => {
    window.clearTimeout(continuousArmTimerRef.current)
    if (!latestContinuousRef.current || latestPendingConfirmRef.current) {
      return
    }

    continuousArmTimerRef.current = window.setTimeout(() => {
      if (!latestContinuousRef.current || latestPendingConfirmRef.current) {
        return
      }
      setAutoListenSignal((current) => current + 1)
    }, delayMs)
  }, [])

  const stopSpeaking = useCallback((shouldRearm = false) => {
    releaseCurrentAudio()
    setIsSpeaking(false)
    if (shouldRearm) {
      armContinuousListening()
    }
  }, [armContinuousListening, releaseCurrentAudio])

  useEffect(() => () => {
    window.clearTimeout(continuousArmTimerRef.current)
    stopSpeaking(false)
  }, [stopSpeaking])

  useEffect(() => {
    if (!voiceEnabled) {
      stopSpeaking(false)
    }
  }, [stopSpeaking, voiceEnabled])

  const speakText = useCallback(async (text) => {
    const spokenText = normalizeSpeechText(text)
    if (!spokenText) {
      armContinuousListening(600)
      return
    }

    if (!voiceEnabled || !speechSupported) {
      armContinuousListening(600)
      return
    }

    stopSpeaking(false)
    setIsSpeaking(true)

    try {
      const response = await fetch(`${apiBase}/tts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: spokenText, voice: selectedVoiceURI }),
      })

      if (!response.ok) {
        throw new Error(`TTS failed with ${response.status}`)
      }

      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const audio = new Audio(url)

      currentAudioRef.current = audio
      currentAudioUrlRef.current = url
      audio.playbackRate = speechRate

      audio.onended = () => {
        stopSpeaking(true)
      }
      audio.onerror = () => {
        stopSpeaking(true)
      }

      await audio.play()
    } catch (error) {
      console.error('Local TTS error:', error)
      stopSpeaking(true)
    }
  }, [armContinuousListening, selectedVoiceURI, speechRate, speechSupported, stopSpeaking, voiceEnabled])

  const speakEntry = useCallback((entry) => {
    const responseText = buildSpeechFromEntry(entry)
    if (responseText) {
      void speakText(responseText)
      return
    }

    armContinuousListening(650)
  }, [armContinuousListening, speakText])

  const addCommand = useCallback((entry) => {
    setCommands((previous) => {
      const updated = [...previous, entry]
      return updated.length > 18 ? updated.slice(-18) : updated
    })

    const data = entry.result?.data
    if (data?.requires_confirmation && data?.operation_id) {
      setPendingConfirm({
        commandId: entry.id,
        operationId: data.operation_id,
        description: data.description || 'This action requires your confirmation.',
      })
      return
    }

    speakEntry(entry)
  }, [speakEntry])

  const handleConfirm = useCallback(async () => {
    if (!pendingConfirm) {
      return
    }

    const { commandId, operationId } = pendingConfirm
    setPendingConfirm(null)

    try {
      const response = await fetch(`${apiBase}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operation_id: operationId }),
      })
      const data = await response.json()

      setCommands((previous) =>
        previous.map((command) =>
          command.id === commandId
            ? {
                ...command,
                confirmed: true,
                result: data.result ?? command.result,
                error: data.success === false
                  ? (data.detail || data.error || 'Confirm failed.')
                  : null,
              }
            : command,
        ),
      )

      if (data.success === false) {
        void speakText(`I could not finish that action. ${data.detail || data.error || 'Confirmation failed.'}`)
        return
      }

      void speakText(data.result?.message || 'Confirmed. The action has been completed.')
    } catch (error) {
      setCommands((previous) =>
        previous.map((command) =>
          command.id === commandId
            ? { ...command, confirmed: false, error: error.message || 'Network error.' }
            : command,
        ),
      )

      void speakText(`I ran into a network error. ${error.message || 'Please try again.'}`)
    }
  }, [pendingConfirm, speakText])

  const handleCancel = useCallback(() => {
    if (!pendingConfirm) {
      return
    }

    const { commandId } = pendingConfirm
    setPendingConfirm(null)

    setCommands((previous) =>
      previous.map((command) =>
        command.id === commandId ? { ...command, cancelled: true } : command,
      ),
    )

    void speakText('Okay. I cancelled that action.')
  }, [pendingConfirm, speakText])

  const handleVoiceTest = useCallback(() => {
    void speakText('Voice replies are ready. I can answer your commands out loud while everything stays local.')
  }, [speakText])

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-brand">
          <span className="app-logo" aria-hidden="true" />
          <div className="app-brand__copy">
            <h1 className="app-title">PC Assistant</h1>
            <p className="app-tagline">A calm command space for your computer.</p>
          </div>
        </div>

        <div className="app-header__actions">
          <label className="theme-toggle">
            <input
              type="checkbox"
              checked={isDarkMode}
              onChange={(event) => setIsDarkMode(event.target.checked)}
            />
            <span className="theme-toggle__track" aria-hidden="true">
              <span className="theme-toggle__thumb" />
            </span>
            <span className="theme-toggle__text">Dark</span>
          </label>

          <div className={`app-header__signal app-header__signal--${heroState.tone}`}>
            <span className="app-header__signal-dot" aria-hidden="true" />
            {heroState.status}
          </div>
        </div>
      </header>

      <main className="app-main">
        <section className="assistant-console" aria-label="Assistant command console">
          <div className="assistant-console__header">
            <div>
              <span className="assistant-console__eyebrow">Now</span>
              <h2 className="assistant-console__title">{heroState.summary}</h2>
            </div>

            <label className="continuous-mode-toggle">
              <input
                type="checkbox"
                checked={isContinuous}
                onChange={(event) => setIsContinuous(event.target.checked)}
              />
              <span className="continuous-mode-toggle__track" aria-hidden="true">
                <span className="continuous-mode-toggle__thumb" />
              </span>
              <span className="continuous-mode-toggle__text">Continuous</span>
            </label>
          </div>

          <p className="assistant-console__detail">{heroState.detail}</p>

          <div className="assistant-console__latest" aria-label="Latest interaction">
            <span className="assistant-console__latest-label">Last</span>
            <span className="assistant-console__latest-text">
              {latestCommand?.intent?.raw_transcript || buildSnapshotLine(latestCommand)}
            </span>
          </div>

          <CommandBar
            onCommand={addCommand}
            isContinuous={isContinuous}
            autoListenSignal={autoListenSignal}
            isSpeaking={isSpeaking}
          />
        </section>

        <section className="activity-area" aria-label="Activity and settings">
          <StatusFeed
            commands={commands}
            title="Recent Activity"
            emptyText="Commands will appear here after you speak or type."
          />

          <div className="quiet-panels">
            <details className="quiet-disclosure">
              <summary>
                <span>Voice settings</span>
                <span className={`quiet-disclosure__status ${isSpeaking ? 'quiet-disclosure__status--active' : ''}`}>
                  {isSpeaking ? 'Speaking' : voiceEnabled ? 'On' : 'Muted'}
                </span>
              </summary>
              <VoicePanel
                speechSupported={speechSupported}
                voiceEnabled={voiceEnabled}
                isSpeaking={isSpeaking}
                voices={voices}
                selectedVoiceURI={selectedVoiceURI}
                speechRate={speechRate}
                onToggleEnabled={() => setVoiceEnabled((current) => !current)}
                onSelectVoice={setSelectedVoiceURI}
                onRateChange={setSpeechRate}
                onStopSpeaking={() => stopSpeaking(isContinuous)}
                onTestVoice={handleVoiceTest}
              />
            </details>

            <details className="quiet-disclosure">
              <summary>
                <span>System details</span>
                <span className="quiet-disclosure__status">Live</span>
              </summary>
              <SystemPanel />
            </details>
          </div>
        </section>
      </main>

      {pendingConfirm && (
        <ConfirmModal
          description={pendingConfirm.description}
          onConfirm={handleConfirm}
          onCancel={handleCancel}
        />
      )}
    </div>
  )
}

function buildHeroState({ latestCommand, pendingConfirm, isSpeaking, voiceEnabled, isContinuous }) {
  if (pendingConfirm) {
    return {
      tone: 'warn',
      status: 'Awaiting confirmation',
      summary: 'Waiting for your approval.',
      detail: pendingConfirm.description,
    }
  }

  if (isSpeaking) {
    return {
      tone: 'active',
      status: 'Speaking back',
      summary: 'Reading the latest result.',
      detail: latestCommand?.result?.message || 'Your assistant replies are active.',
    }
  }

  if (latestCommand?.error) {
    return {
      tone: 'danger',
      status: 'Needs attention',
      summary: 'The last action needs attention.',
      detail: latestCommand.error,
    }
  }

  if (latestCommand?.intent?.intent === 'clarify') {
    return {
      tone: 'warn',
      status: 'Needs clarification',
      summary: latestCommand.result?.message || 'A little more detail is needed.',
      detail: latestCommand.result?.data?.follow_up || 'Try repeating the command a little more clearly.',
    }
  }

  if (latestCommand?.result?.message) {
    return {
      tone: 'ready',
      status: isContinuous ? 'Continuous mode active' : 'Ready again',
      summary: latestCommand.result.message,
      detail: voiceEnabled ? 'Voice replies are on.' : 'Voice replies are muted.',
    }
  }

  return {
    tone: 'ready',
    status: isContinuous ? 'Continuous mode active' : 'Standing by',
    summary: 'Ready for a command.',
    detail: 'Use the mic or type below.',
  }
}

function buildSnapshotLine(entry) {
  if (!entry) {
    return 'No recent command.'
  }

  if (entry.error) {
    return entry.error
  }

  if (entry.result?.message) {
    return entry.result.message
  }

  return 'Ready for the next command.'
}

function buildSpeechFromEntry(entry) {
  if (!entry) {
    return ''
  }

  if (entry.error) {
    return `I ran into an error. ${entry.error}`
  }

  const resultData = entry.result?.data ?? {}
  if (resultData.requires_confirmation) {
    return `Please confirm this action. ${resultData.description || entry.result?.message || 'This action needs your approval.'}`
  }

  if (resultData.requires_clarification) {
    return resultData.follow_up || entry.result?.message || 'I need a little more detail.'
  }

  if (entry.intent?.intent === 'system_query') {
    const systemSummary = summarizeSystemQuery(resultData)
    if (systemSummary) {
      return systemSummary
    }
  }

  if (entry.intent?.intent === 'search_pc') {
    const searchSummary = summarizeSearchResults(resultData, entry.result?.message)
    if (searchSummary) {
      return searchSummary
    }
  }

  return entry.result?.message || ''
}

function normalizeSpeechText(text) {
  if (!text) {
    return ''
  }

  return String(text)
    .replace(/\s+/g, ' ')
    .replace(/[{}[\]_*`]/g, ' ')
    .trim()
}

function summarizeSystemQuery(data) {
  if (!data || typeof data !== 'object') {
    return ''
  }

  if (typeof data.active_window === 'string' && data.active_window.trim()) {
    return `The active window is ${data.active_window}.`
  }

  if (typeof data.cpu_percent === 'number') {
    return `CPU usage is ${data.cpu_percent.toFixed(1)} percent.`
  }

  if (data.memory && typeof data.memory === 'object') {
    const used = Number(data.memory.used_gb)
    const total = Number(data.memory.total_gb)
    const percent = Number(data.memory.percent)
    if (Number.isFinite(used) && Number.isFinite(total)) {
      const percentText = Number.isFinite(percent) ? `, about ${percent.toFixed(1)} percent` : ''
      return `Memory usage is ${used.toFixed(1)} out of ${total.toFixed(1)} gigabytes${percentText}.`
    }
  }

  if (data.disk && typeof data.disk === 'object') {
    const used = Number(data.disk.used_gb)
    const total = Number(data.disk.total_gb)
    const percent = Number(data.disk.percent)
    if (Number.isFinite(used) && Number.isFinite(total)) {
      const percentText = Number.isFinite(percent) ? `, about ${percent.toFixed(1)} percent` : ''
      return `Disk usage is ${used.toFixed(1)} out of ${total.toFixed(1)} gigabytes${percentText}.`
    }
  }

  if (Array.isArray(data.running_apps) && data.running_apps.length > 0) {
    const preview = data.running_apps
      .slice(0, 4)
      .map((name) => String(name).replace(/\.exe$/i, ''))
      .join(', ')
    return `I found ${data.running_apps.length} running apps. Some of them are ${preview}.`
  }

  return ''
}

function summarizeSearchResults(data, fallbackMessage) {
  if (!data || typeof data !== 'object') {
    return fallbackMessage || ''
  }

  const count = Number(data.count)
  const results = Array.isArray(data.results) ? data.results : []
  if (!Number.isFinite(count)) {
    return fallbackMessage || ''
  }

  if (count <= 0) {
    return fallbackMessage || 'I did not find any matching files.'
  }

  const preview = results
    .slice(0, 3)
    .map((result) => {
      const segments = String(result).split(/[\\/]/)
      return segments[segments.length - 1]
    })
    .join(', ')

  if (!preview) {
    return fallbackMessage || `I found ${count} matching files.`
  }

  return `I found ${count} matching files. The first few are ${preview}.`
}

function readStoredBoolean(key, fallback) {
  if (typeof window === 'undefined') {
    return fallback
  }

  const rawValue = window.localStorage.getItem(key)
  if (rawValue === null) {
    return fallback
  }

  return rawValue === 'true'
}

function readStoredNumber(key, fallback) {
  if (typeof window === 'undefined') {
    return fallback
  }

  const rawValue = window.localStorage.getItem(key)
  const parsedValue = Number(rawValue)
  return Number.isFinite(parsedValue) ? parsedValue : fallback
}

function readStoredString(key, fallback) {
  if (typeof window === 'undefined') {
    return fallback
  }

  return window.localStorage.getItem(key) ?? fallback
}

function writeStoredBoolean(key, value) {
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(key, value ? 'true' : 'false')
  }
}

function writeStoredString(key, value) {
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(key, value)
  }
}
