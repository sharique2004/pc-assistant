import { useState, useRef, useCallback, useEffect } from 'react'
import apiBase from '../apiBase.js'
import './CommandBar.css'

const MAX_RECORDING_MS = 10000
const TARGET_SAMPLE_RATE = 16000
const LOW_SIGNAL_LEVEL = 0.0003

/**
 * CommandBar - browser microphone recorder for voice commands.
 *
 * Clicking once starts microphone capture in the browser. Clicking again stops
 * recording and uploads clean WAV audio to the Flask backend, which then runs
 * local Whisper transcription and local Ollama intent parsing.
 */
export default function CommandBar({ onCommand, isContinuous, autoListenSignal, isSpeaking }) {
  const [uiState, setUiState] = useState('idle')
  const [inputLevel, setInputLevel] = useState(0)
  const [audioInputs, setAudioInputs] = useState([])
  const [selectedDeviceId, setSelectedDeviceId] = useState('')
  const [textCommand, setTextCommand] = useState('')
  const [textSending, setTextSending] = useState(false)

  const mediaStreamRef = useRef(null)
  const audioContextRef = useRef(null)
  const sourceNodeRef = useRef(null)
  const processorNodeRef = useRef(null)
  const silentGainNodeRef = useRef(null)
  const audioChunksRef = useRef([])
  const sourceSampleRateRef = useRef(TARGET_SAMPLE_RATE)
  const levelRef = useRef(0)
  const peakLevelRef = useRef(0)
  const animationFrameRef = useRef(null)
  const autoStopTimerRef = useRef(null)
  const resetTimerRef = useRef(null)
  const lastAutoListenSignalRef = useRef(0)

  const resetUiSoon = useCallback(() => {
    clearTimeout(resetTimerRef.current)
    resetTimerRef.current = setTimeout(() => setUiState('idle'), 1400)
  }, [])

  const createErrorEntry = useCallback((message, trigger = 'browser_voice') => ({
    id: `cmd-${Date.now()}`,
    trigger,
    intent: null,
    result: null,
    error: message,
    timestamp: new Date().toISOString(),
  }), [])

  const refreshAudioInputs = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) {
      return
    }

    try {
      const devices = await navigator.mediaDevices.enumerateDevices()
      const microphones = devices.filter((device) => device.kind === 'audioinput')
      setAudioInputs(microphones)
      setSelectedDeviceId((current) => (
        current && microphones.some((device) => device.deviceId === current)
          ? current
          : ''
      ))
    } catch {
      setAudioInputs([])
    }
  }, [])

  const animateInputLevel = useCallback(() => {
    setInputLevel((previous) => clampLevel(Math.max(levelRef.current, previous * 0.78)))
    animationFrameRef.current = window.requestAnimationFrame(animateInputLevel)
  }, [])

  const cleanupRecorder = useCallback(() => {
    clearTimeout(autoStopTimerRef.current)
    clearTimeout(resetTimerRef.current)

    if (animationFrameRef.current) {
      window.cancelAnimationFrame(animationFrameRef.current)
    }

    if (processorNodeRef.current) {
      processorNodeRef.current.onaudioprocess = null
      processorNodeRef.current.disconnect()
    }

    if (sourceNodeRef.current) {
      sourceNodeRef.current.disconnect()
    }

    if (silentGainNodeRef.current) {
      silentGainNodeRef.current.disconnect()
    }

    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach((track) => track.stop())
    }

    if (audioContextRef.current && audioContextRef.current.state !== 'closed') {
      void audioContextRef.current.close()
    }

    mediaStreamRef.current = null
    audioContextRef.current = null
    sourceNodeRef.current = null
    processorNodeRef.current = null
    silentGainNodeRef.current = null
    audioChunksRef.current = []
    sourceSampleRateRef.current = TARGET_SAMPLE_RATE
    levelRef.current = 0
    peakLevelRef.current = 0
    animationFrameRef.current = null
    setInputLevel(0)
  }, [])

  const uploadAudio = useCallback(async (audioBlob) => {
    setUiState('processing')

    const entry = {
      id: `cmd-${Date.now()}`,
      trigger: 'browser_voice',
      intent: null,
      result: null,
      error: null,
      timestamp: new Date().toISOString(),
    }

    const formData = new FormData()
    formData.append('trigger', 'browser_voice')
    formData.append('audio', audioBlob, `command-${Date.now()}.wav`)

    try {
      const response = await fetch(`${apiBase}/command`, {
        method: 'POST',
        body: formData,
      })
      const data = await response.json()

      if (!response.ok || data.success === false) {
        entry.error = data.detail || data.error || `HTTP ${response.status}`
        setUiState('error')
      } else {
        entry.intent = data.intent ?? null
        entry.result = data.result ?? null
        setUiState('success')
      }
    } catch (err) {
      entry.error = err.message || 'Network error'
      setUiState('error')
    }

    onCommand(entry)
    resetUiSoon()
  }, [onCommand, resetUiSoon])

  const submitTextCommand = useCallback(async (event) => {
    event.preventDefault()

    const commandText = textCommand.trim()
    if (!commandText || textSending) {
      return
    }

    const entry = {
      id: `cmd-${Date.now()}`,
      trigger: 'typed_text',
      intent: null,
      result: null,
      error: null,
      timestamp: new Date().toISOString(),
    }

    setTextSending(true)

    try {
      const response = await fetch(`${apiBase}/command`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ trigger: 'typed_text', text: commandText }),
      })
      const data = await response.json()

      if (!response.ok || data.success === false) {
        entry.error = data.detail || data.error || `HTTP ${response.status}`
      } else {
        entry.intent = data.intent ?? null
        entry.result = data.result ?? null
        setTextCommand('')
      }
    } catch (err) {
      entry.error = err.message || 'Network error'
    } finally {
      setTextSending(false)
    }

    onCommand(entry)
  }, [onCommand, textCommand, textSending])

  const stopRecording = useCallback(async () => {
    clearTimeout(autoStopTimerRef.current)

    const recordedChunks = audioChunksRef.current.slice()
    const sourceSampleRate = sourceSampleRateRef.current
    const peakLevel = peakLevelRef.current

    cleanupRecorder()

    if (recordedChunks.length === 0) {
      setUiState('error')
      onCommand(createErrorEntry('No audio was captured. Please try again.'))
      resetUiSoon()
      return
    }

    try {
      const wavBlob = encodeWaveBlob(recordedChunks, sourceSampleRate, TARGET_SAMPLE_RATE)
      await uploadAudio(wavBlob)
    } catch (err) {
      setUiState('error')
      onCommand(createErrorEntry(err.message || 'Audio processing failed.'))
      resetUiSoon()
    }
  }, [cleanupRecorder, createErrorEntry, onCommand, resetUiSoon, uploadAudio])

  const startRecording = useCallback(async () => {
    if (mediaStreamRef.current || uiState === 'recording' || uiState === 'processing') {
      return
    }

    if (!navigator.mediaDevices?.getUserMedia) {
      setUiState('error')
      onCommand(createErrorEntry('This browser does not support microphone recording.'))
      resetUiSoon()
      return
    }

    const AudioContextClass = window.AudioContext || window.webkitAudioContext
    if (!AudioContextClass) {
      setUiState('error')
      onCommand(createErrorEntry('Web Audio is not available in this browser.'))
      resetUiSoon()
      return
    }

    try {
      const audioConstraints = {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      }

      if (selectedDeviceId) {
        audioConstraints.deviceId = { exact: selectedDeviceId }
      }

      const stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints })
      const audioContext = new AudioContextClass()
      const sourceNode = audioContext.createMediaStreamSource(stream)
      const processorNode = audioContext.createScriptProcessor(4096, 1, 1)
      const silentGainNode = audioContext.createGain()

      silentGainNode.gain.value = 0

      processorNode.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0)
        const chunk = new Float32Array(input.length)
        chunk.set(input)
        audioChunksRef.current.push(chunk)

        let sumSquares = 0
        for (let index = 0; index < input.length; index += 1) {
          sumSquares += input[index] * input[index]
        }

        const rms = Math.sqrt(sumSquares / input.length)
        levelRef.current = rms
        peakLevelRef.current = Math.max(peakLevelRef.current, rms)
      }

      audioChunksRef.current = []
      sourceSampleRateRef.current = audioContext.sampleRate
      levelRef.current = 0
      peakLevelRef.current = 0

      mediaStreamRef.current = stream
      audioContextRef.current = audioContext
      sourceNodeRef.current = sourceNode
      processorNodeRef.current = processorNode
      silentGainNodeRef.current = silentGainNode

      sourceNode.connect(processorNode)
      processorNode.connect(silentGainNode)
      silentGainNode.connect(audioContext.destination)

      if (audioContext.state === 'suspended') {
        await audioContext.resume()
      }

      await refreshAudioInputs()

      setUiState('recording')
      animationFrameRef.current = window.requestAnimationFrame(animateInputLevel)
      autoStopTimerRef.current = setTimeout(() => {
        void stopRecording()
      }, MAX_RECORDING_MS)
    } catch (err) {
      cleanupRecorder()
      setUiState('error')
      onCommand(createErrorEntry(err.message || 'Microphone access was denied.'))
      resetUiSoon()
    }
  }, [
    animateInputLevel,
    cleanupRecorder,
    createErrorEntry,
    onCommand,
    refreshAudioInputs,
    resetUiSoon,
    selectedDeviceId,
    stopRecording,
    uiState,
  ])

  useEffect(() => {
    if (
      isContinuous
      && autoListenSignal > 0
      && autoListenSignal !== lastAutoListenSignalRef.current
      && uiState === 'idle'
      && !isSpeaking
    ) {
      lastAutoListenSignalRef.current = autoListenSignal
      void startRecording()
    }
  }, [autoListenSignal, isContinuous, uiState, startRecording, isSpeaking])

  const handleClick = useCallback((event) => {
    event.preventDefault()

    if (uiState === 'recording') {
      void stopRecording()
      return
    }

    if (uiState !== 'idle') {
      return
    }

    void startRecording()
  }, [startRecording, stopRecording, uiState])

  useEffect(() => {
    void refreshAudioInputs()

    const mediaDevices = navigator.mediaDevices
    if (mediaDevices?.addEventListener) {
      mediaDevices.addEventListener('devicechange', refreshAudioInputs)
    }

    return () => {
      cleanupRecorder()
      if (mediaDevices?.removeEventListener) {
        mediaDevices.removeEventListener('devicechange', refreshAudioInputs)
      }
    }
  }, [cleanupRecorder, refreshAudioInputs])

  const labels = {
    idle: 'Speak',
    recording: 'Send Command',
    processing: 'Working...',
    success: 'Ready',
    error: 'Try Again',
  }

  const statusText = {
    idle: 'Ready.',
    recording: inputLevel > LOW_SIGNAL_LEVEL
      ? 'Listening. Tap again to send.'
      : 'Listening for your voice.',
    processing: 'Working...',
    success: 'Done.',
    error: 'Try again.',
  }

  const isDisabled = uiState === 'processing'

  return (
    <section className={`command-bar command-bar--${uiState}`} aria-label="Voice command trigger">
      <div className="command-bar__primary">
        <button
          className={`speak-btn speak-btn--${uiState}`}
          onClick={handleClick}
          disabled={isDisabled}
          aria-pressed={uiState === 'recording'}
          aria-label={labels[uiState]}
          aria-busy={uiState === 'recording' || uiState === 'processing'}
        >
          <span className="speak-btn__icon" aria-hidden="true">
            {uiState === 'recording' && <PulseRing />}
            {uiState === 'processing' && <span className="spinner" />}
            {(uiState === 'idle' || uiState === 'success' || uiState === 'error') && <MicIcon />}
          </span>
          <span className="speak-btn__label">{labels[uiState]}</span>
        </button>

        <form className="command-bar__text-form" onSubmit={submitTextCommand}>
          <label className="command-bar__text-label" htmlFor="typed-command">
            Command
          </label>
          <div className="command-bar__text-row">
            <input
              id="typed-command"
              className="command-bar__text-input"
              type="text"
              value={textCommand}
              onChange={(event) => setTextCommand(event.target.value)}
              placeholder="Type a command"
              disabled={textSending}
            />
            <button
              className="command-bar__text-submit"
              type="submit"
              disabled={!textCommand.trim() || textSending}
            >
              {textSending ? 'Working...' : 'Send'}
            </button>
          </div>
        </form>
      </div>

      <div className="command-bar__status" aria-live="polite">
        <span className="command-bar__status-text">{statusText[uiState]}</span>
        {uiState === 'recording' && (
          <div className="command-bar__meter-track" aria-hidden="true">
            <div
              className="command-bar__meter-fill"
              style={{ transform: `scaleX(${clampLevel(inputLevel * 10)})` }}
            />
          </div>
        )}
      </div>

      <details className="command-bar__advanced">
        <summary>Microphone</summary>
        <div className="command-bar__controls">
          <label className="command-bar__device">
            <span className="command-bar__control-label">Input</span>
            <select
              className="command-bar__select"
              value={selectedDeviceId}
              onChange={(event) => setSelectedDeviceId(event.target.value)}
              disabled={uiState === 'recording' || uiState === 'processing'}
            >
              <option value="">System default microphone</option>
              {audioInputs.map((device, index) => (
                <option key={device.deviceId || `mic-${index}`} value={device.deviceId}>
                  {device.label || `Microphone ${index + 1}`}
                </option>
              ))}
            </select>
          </label>

          <div className="command-bar__meter" aria-live="polite">
            <span className="command-bar__control-label">Level</span>
            <div className="command-bar__meter-track" aria-hidden="true">
              <div
                className="command-bar__meter-fill"
                style={{ transform: `scaleX(${clampLevel(inputLevel * 10)})` }}
              />
            </div>
          </div>
        </div>
      </details>
    </section>
  )
}

function MicIcon() {
  return (
    <svg
      width="26"
      height="26"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="9" y="2" width="6" height="11" rx="3" />
      <path d="M5 10a7 7 0 0 0 14 0" />
      <line x1="12" y1="19" x2="12" y2="22" />
      <line x1="8" y1="22" x2="16" y2="22" />
    </svg>
  )
}

function PulseRing() {
  return (
    <span className="pulse-ring" aria-hidden="true">
      <MicIcon />
    </span>
  )
}

function clampLevel(value) {
  return Math.max(0, Math.min(1, value))
}

function encodeWaveBlob(chunks, sourceSampleRate, targetSampleRate) {
  const merged = mergeAudioChunks(chunks)
  const downsampled = downsampleBuffer(merged, sourceSampleRate, targetSampleRate)
  const normalized = normalizeAudioSamples(downsampled)
  const wavBytes = encodeWaveBytes(normalized, targetSampleRate)
  return new Blob([wavBytes], { type: 'audio/wav' })
}

function mergeAudioChunks(chunks) {
  const totalLength = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  const merged = new Float32Array(totalLength)
  let offset = 0

  chunks.forEach((chunk) => {
    merged.set(chunk, offset)
    offset += chunk.length
  })

  return merged
}

function downsampleBuffer(buffer, sourceSampleRate, targetSampleRate) {
  if (sourceSampleRate === targetSampleRate) {
    return buffer
  }

  const sampleRateRatio = sourceSampleRate / targetSampleRate
  const newLength = Math.max(1, Math.round(buffer.length / sampleRateRatio))
  const downsampled = new Float32Array(newLength)
  let offsetResult = 0
  let offsetBuffer = 0

  while (offsetResult < downsampled.length) {
    const nextOffsetBuffer = Math.min(
      buffer.length,
      Math.round((offsetResult + 1) * sampleRateRatio),
    )

    let sum = 0
    let count = 0
    for (let index = offsetBuffer; index < nextOffsetBuffer; index += 1) {
      sum += buffer[index]
      count += 1
    }

    downsampled[offsetResult] = count > 0 ? sum / count : 0
    offsetResult += 1
    offsetBuffer = nextOffsetBuffer
  }

  return downsampled
}

function normalizeAudioSamples(buffer) {
  let peak = 0
  for (let index = 0; index < buffer.length; index += 1) {
    peak = Math.max(peak, Math.abs(buffer[index]))
  }

  if (peak <= 0) {
    return buffer
  }

  const gain = Math.min(8, 0.92 / peak)
  if (gain <= 1.05) {
    return buffer
  }

  const normalized = new Float32Array(buffer.length)
  for (let index = 0; index < buffer.length; index += 1) {
    normalized[index] = Math.max(-1, Math.min(1, buffer[index] * gain))
  }

  return normalized
}

function encodeWaveBytes(samples, sampleRate) {
  const bytesPerSample = 2
  const buffer = new ArrayBuffer(44 + samples.length * bytesPerSample)
  const view = new DataView(buffer)

  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + samples.length * bytesPerSample, true)
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * bytesPerSample, true)
  view.setUint16(32, bytesPerSample, true)
  view.setUint16(34, 16, true)
  writeAscii(view, 36, 'data')
  view.setUint32(40, samples.length * bytesPerSample, true)

  let offset = 44
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]))
    view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true)
    offset += bytesPerSample
  }

  return buffer
}

function writeAscii(view, offset, value) {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index))
  }
}
