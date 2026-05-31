import { useState, useRef, useCallback, useEffect } from 'react'
import BibiOrb from './BibiOrb.jsx'
import Browser, { BrowserIdle } from './Browser.jsx'
import apiBase from '../apiBase.js'
import './bibi.css'

const MAX_RECORDING_MS = 12000
const TARGET_SAMPLE_RATE = 16000
const POLL_MS = 600

// Example utterances — these hit the REAL backend (plan → act / answer).
const SUGGESTIONS = [
  { glyph: '🌐', text: 'Open YouTube and Gmail' },
  { glyph: '🌤', text: "What's the weather in Dubai?" },
  { glyph: '✈', text: 'Book me a flight to Dubai' },
  { glyph: '⌨', text: 'Search the best mechanical keyboards' },
]

// ─────────────────────────────────────────────────────────
// Chirpy beeps
// ─────────────────────────────────────────────────────────
function Beeps({ trigger }) {
  const [beeps, setBeeps] = useState([])
  const phrases = ['beep!', 'bweep~', '— boop —', 'ok!', 'bdbdbd', 'wheet!', 'pop']
  useEffect(() => {
    if (!trigger) return
    const id = Date.now() + Math.random()
    const phrase = phrases[Math.floor(Math.random() * phrases.length)]
    const offX = (Math.random() - 0.5) * 60
    setBeeps((b) => [...b, { id, phrase, offX }])
    const t = setTimeout(() => setBeeps((b) => b.filter((x) => x.id !== id)), 1300)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trigger])
  return (
    <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
      {beeps.map((b) => (
        <span key={b.id} className="beep" style={{ left: `calc(50% + ${b.offX}px)`, top: '20%' }}>{b.phrase}</span>
      ))}
    </div>
  )
}

export default function BibiApp() {
  const [phase, setPhase] = useState('idle') // idle | listening | thinking | acting | done
  const [workflow, setWorkflow] = useState(null)
  const [transcript, setTranscript] = useState('')
  const [statusBanner, setStatusBanner] = useState('')
  const [typedCommand, setTypedCommand] = useState('')
  const [beepTrigger, setBeepTrigger] = useState(0)
  const [orbStyle] = useState('rings')

  const [wakeOn, setWakeOn] = useState(false)
  const [wakeStatus, setWakeStatus] = useState('')

  const [pageType, setPageType] = useState('')
  const [previewLive, setPreviewLive] = useState(false)
  const [shotTick, setShotTick] = useState(0)

  // refs
  const pollRef = useRef(null)
  const hydratedRef = useRef(false)   // skip replaying session backlog on refresh
  const lastSpokenRef = useRef(0)
  const lastWakePingRef = useRef(0)
  const lastProcessPingRef = useRef(0)
  const lastWfStatusRef = useRef('')
  const speakQueueRef = useRef([])
  const speakingRef = useRef(false)
  const audioCtxRef = useRef(null)

  // recorder refs
  const mediaStreamRef = useRef(null)
  const audioContextRef = useRef(null)
  const sourceNodeRef = useRef(null)
  const processorNodeRef = useRef(null)
  const silentGainNodeRef = useRef(null)
  const audioChunksRef = useRef([])
  const sourceSampleRateRef = useRef(TARGET_SAMPLE_RATE)
  const autoStopTimerRef = useRef(null)
  const isRecordingRef = useRef(false)

  // ─── Audio cues: distinct tones for each state ──────────
  const playTones = useCallback((tones) => {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext
      if (!Ctx) return
      if (!audioCtxRef.current) audioCtxRef.current = new Ctx()
      const ctx = audioCtxRef.current
      if (ctx.state === 'suspended') ctx.resume()
      const now = ctx.currentTime
      tones.forEach(([freq, t, dur = 0.16]) => {
        const osc = ctx.createOscillator()
        const gain = ctx.createGain()
        osc.type = 'sine'
        osc.frequency.value = freq
        gain.gain.setValueAtTime(0.0001, now + t)
        gain.gain.exponentialRampToValueAtTime(0.22, now + t + 0.02)
        gain.gain.exponentialRampToValueAtTime(0.0001, now + t + dur)
        osc.connect(gain); gain.connect(ctx.destination)
        osc.start(now + t); osc.stop(now + t + dur + 0.02)
      })
    } catch { /* ignore */ }
  }, [])

  // Rising two-tone = "I'm listening".
  const playChime = useCallback(() => playTones([[660, 0], [880, 0.12]]), [playTones])
  // Falling two-tone = "got it, stopped listening, working on it".
  const playWorking = useCallback(() => playTones([[700, 0], [440, 0.13]]), [playTones])
  // Soft single low tone = "done".
  const playDone = useCallback(() => playTones([[523, 0, 0.22]]), [playTones])

  const drainSpeakQueue = useCallback(async () => {
    if (speakingRef.current) return
    speakingRef.current = true
    while (speakQueueRef.current.length) {
      const text = speakQueueRef.current.shift()
      try {
        const res = await fetch(`${apiBase}/tts`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        })
        if (res.ok) {
          const blob = await res.blob()
          const url = URL.createObjectURL(blob)
          const audio = new Audio(url)
          // eslint-disable-next-line no-await-in-loop
          await new Promise((resolve) => {
            audio.onended = resolve
            audio.onerror = resolve
            audio.play().catch(resolve)
          })
          URL.revokeObjectURL(url)
        }
      } catch { /* ignore TTS errors */ }
    }
    speakingRef.current = false
  }, [])

  // ─── Poll unified agent status ──────────────────────────
  const poll = useCallback(async () => {
    try {
      const res = await fetch(`${apiBase}/agent/status`)
      if (!res.ok) return
      const data = await res.json()

      setWakeOn(Boolean(data.listening))
      setWakeStatus(data.wake_status || '')

      // On the FIRST poll after a (re)load, baseline the counters to "now" so
      // we DON'T replay/re-speak the whole session's backlog on refresh.
      if (!hydratedRef.current) {
        hydratedRef.current = true
        const speak0 = Array.isArray(data.speak) ? data.speak : []
        lastSpokenRef.current = speak0.length ? speak0[speak0.length - 1].id : 0
        lastWakePingRef.current = typeof data.wake_ping === 'number' ? data.wake_ping : 0
        lastProcessPingRef.current = typeof data.process_ping === 'number' ? data.process_ping : 0
        lastWfStatusRef.current = data.workflow?.status || ''
        // Reflect existing state silently (no audio, no chime).
        setWorkflow(data.workflow || null)
        if (data.workflow) {
          setTranscript(data.workflow.transcript || '')
          setStatusBanner(data.workflow.message || '')
          setPageType(data.workflow.kind || '')
          const st0 = data.workflow.status
          setPhase(st0 === 'planning' ? 'thinking' : st0 === 'running' ? 'acting'
                   : st0 === 'done' || st0 === 'error' ? 'done' : 'idle')
        }
        return
      }

      // Wake chime when the backend signals a fresh wake → "I'm listening".
      if (typeof data.wake_ping === 'number' && data.wake_ping > lastWakePingRef.current) {
        lastWakePingRef.current = data.wake_ping
        playChime()
        setBeepTrigger((b) => b + 1)
        setPhase('listening')
      }

      // "Working" chime when capture stops and processing begins.
      if (typeof data.process_ping === 'number' && data.process_ping > lastProcessPingRef.current) {
        lastProcessPingRef.current = data.process_ping
        playWorking()
        setBeepTrigger((b) => b + 1)
        setPhase('thinking')
      }

      // Speak any new lines.
      const speak = Array.isArray(data.speak) ? data.speak : []
      const fresh = speak.filter((s) => s.id > lastSpokenRef.current)
      if (fresh.length) {
        lastSpokenRef.current = fresh[fresh.length - 1].id
        fresh.forEach((s) => speakQueueRef.current.push(s.text))
        void drainSpeakQueue()
      }

      const wf = data.workflow
      setWorkflow(wf || null)
      if (wf) {
        setTranscript(wf.transcript || '')
        setStatusBanner(wf.message || '')
        setPageType(wf.kind || '')
        const st = wf.status
        if (st !== lastWfStatusRef.current) {
          lastWfStatusRef.current = st
          if (st === 'done') { setBeepTrigger((b) => b + 1); playDone() }
        }
        if (st === 'planning') setPhase('thinking')
        else if (st === 'running') setPhase('acting')
        else if (st === 'done') setPhase('done')
        else if (st === 'error') setPhase('done')
      }
    } catch { /* backend unreachable */ }
  }, [drainSpeakQueue, playChime, playWorking, playDone])

  // Preview polling — shows what Bibi sees on your screen while it acts.
  const pollPreview = useCallback(async () => {
    try {
      const res = await fetch(`${apiBase}/screen/shot?probe=1`, { method: 'GET' })
      const live = res.status === 200
      setPreviewLive(live)
      if (live) setShotTick((t) => t + 1)
    } catch { setPreviewLive(false) }
  }, [])

  useEffect(() => {
    pollRef.current = setInterval(() => { void poll() }, POLL_MS)
    void poll()
    return () => { clearInterval(pollRef.current) }
  }, [poll])

  // Unlock audio output on the very first interaction with the window, so the
  // chime + Bibi's spoken replies play without the browser's autoplay block.
  useEffect(() => {
    const unlock = () => {
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext
        if (Ctx) { if (!audioCtxRef.current) audioCtxRef.current = new Ctx(); void audioCtxRef.current.resume() }
        const a = new Audio(); a.muted = true; const p = a.play(); if (p) p.catch(() => {})
      } catch { /* ignore */ }
      window.removeEventListener('pointerdown', unlock)
      window.removeEventListener('keydown', unlock)
    }
    window.addEventListener('pointerdown', unlock)
    window.addEventListener('keydown', unlock)
    return () => { window.removeEventListener('pointerdown', unlock); window.removeEventListener('keydown', unlock) }
  }, [])

  // ─── Send commands ──────────────────────────────────────
  const runText = useCallback(async (text) => {
    const cmd = (text || '').trim()
    if (!cmd) return
    setTranscript(cmd)
    setPhase('thinking')
    setStatusBanner('Thinking…')
    setBeepTrigger((b) => b + 1)
    try {
      await fetch(`${apiBase}/agent/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: cmd }),
      })
    } catch (err) {
      setStatusBanner(err.message || 'Could not reach Bibi.')
      setPhase('done')
    }
  }, [])

  // ─── Mic recorder → /agent/voice ────────────────────────
  const cleanupRecorder = useCallback(() => {
    clearTimeout(autoStopTimerRef.current)
    if (processorNodeRef.current) { processorNodeRef.current.onaudioprocess = null; processorNodeRef.current.disconnect() }
    if (sourceNodeRef.current) sourceNodeRef.current.disconnect()
    if (silentGainNodeRef.current) silentGainNodeRef.current.disconnect()
    if (mediaStreamRef.current) mediaStreamRef.current.getTracks().forEach((t) => t.stop())
    if (audioContextRef.current && audioContextRef.current.state !== 'closed') void audioContextRef.current.close()
    mediaStreamRef.current = null; audioContextRef.current = null; sourceNodeRef.current = null
    processorNodeRef.current = null; silentGainNodeRef.current = null; audioChunksRef.current = []
    sourceSampleRateRef.current = TARGET_SAMPLE_RATE; isRecordingRef.current = false
  }, [])

  const stopRecording = useCallback(async () => {
    clearTimeout(autoStopTimerRef.current)
    const chunks = audioChunksRef.current.slice()
    const sr = sourceSampleRateRef.current
    cleanupRecorder()
    if (!chunks.length) { setStatusBanner('No audio captured.'); setPhase('idle'); return }
    playWorking()  // "got it — stopped listening, working on it"
    setPhase('thinking'); setStatusBanner('Transcribing…')
    try {
      const wav = encodeWaveBlob(chunks, sr, TARGET_SAMPLE_RATE)
      const fd = new FormData()
      fd.append('audio', wav, `cmd-${Date.now()}.wav`)
      const res = await fetch(`${apiBase}/agent/voice`, { method: 'POST', body: fd })
      const data = await res.json()
      if (data.transcript) setTranscript(data.transcript)
      else setStatusBanner('I didn’t catch that — try again.')
    } catch (err) {
      setStatusBanner(err.message || 'Voice failed.'); setPhase('done')
    }
  }, [cleanupRecorder, playWorking])

  const startRecording = useCallback(async () => {
    if (isRecordingRef.current) return
    const Ctx = window.AudioContext || window.webkitAudioContext
    if (!navigator.mediaDevices?.getUserMedia || !Ctx) { setStatusBanner('Mic not supported here.'); return }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
      const ctx = new Ctx()
      const src = ctx.createMediaStreamSource(stream)
      const proc = ctx.createScriptProcessor(4096, 1, 1)
      const sink = ctx.createGain(); sink.gain.value = 0
      proc.onaudioprocess = (e) => {
        const input = e.inputBuffer.getChannelData(0)
        const chunk = new Float32Array(input.length); chunk.set(input)
        audioChunksRef.current.push(chunk)
      }
      audioChunksRef.current = []; sourceSampleRateRef.current = ctx.sampleRate
      mediaStreamRef.current = stream; audioContextRef.current = ctx
      sourceNodeRef.current = src; processorNodeRef.current = proc; silentGainNodeRef.current = sink
      src.connect(proc); proc.connect(sink); sink.connect(ctx.destination)
      if (ctx.state === 'suspended') await ctx.resume()
      isRecordingRef.current = true
      setPhase('listening'); setTranscript(''); setStatusBanner('Listening… tap mic to send')
      setBeepTrigger((b) => b + 1)
      autoStopTimerRef.current = setTimeout(() => { void stopRecording() }, MAX_RECORDING_MS)
    } catch (err) {
      cleanupRecorder(); setStatusBanner(err.message || 'Mic access denied.'); setPhase('idle')
    }
  }, [cleanupRecorder, stopRecording])

  // Push-to-talk: the BACKEND records one command (pausing the wake listener so
  // there's no mic contention), transcribes it, and runs it. Far more reliable
  // than the browser mic or the always-on wake word.
  const micBusyRef = useRef(false)
  const isRecording = phase === 'listening'
  const handleMic = useCallback(async () => {
    if (micBusyRef.current) return
    micBusyRef.current = true
    setPhase('listening'); setStatusBanner('Listening… speak now'); setTranscript(''); setBeepTrigger((b) => b + 1)
    try {
      const res = await fetch(`${apiBase}/listen`, { method: 'POST' })
      const data = await res.json()
      if (data.transcript) setTranscript(data.transcript)
      else { setStatusBanner("I didn't catch that — try again."); setPhase('idle') }
    } catch (err) {
      setStatusBanner('Mic error: ' + (err.message || '')); setPhase('idle')
    } finally {
      micBusyRef.current = false
    }
  }, [])

  // ─── Wake toggle ────────────────────────────────────────
  const toggleWake = useCallback(async () => {
    try {
      if (wakeOn) { await fetch(`${apiBase}/wake/stop`, { method: 'POST' }); setWakeOn(false) }
      else {
        // user gesture: unlock audio for chime + TTS
        try { const C = window.AudioContext || window.webkitAudioContext; if (C) { if (!audioCtxRef.current) audioCtxRef.current = new C(); audioCtxRef.current.resume() } } catch { /* */ }
        await fetch(`${apiBase}/wake/start`, { method: 'POST' }); setWakeOn(true)
      }
    } catch (err) { setStatusBanner(err.message || 'Wake toggle failed.') }
  }, [wakeOn])

  const reset = useCallback(() => {
    setWorkflow(null); setPhase(wakeOn ? 'idle' : 'idle'); setTranscript('')
    setStatusBanner(''); lastWfStatusRef.current = ''
  }, [wakeOn])

  // ─── Derived render bits ────────────────────────────────
  const statusLabel = { idle: 'Standby', listening: 'Listening', thinking: 'Thinking', acting: 'Acting', done: 'Done' }[phase]
  const isQuestion = pageType === 'question'
  const tasks = workflow?.tasks || []
  const hasTasks = tasks.length > 0

  return (
    <div className="bibi-app">
      {/* ── Left rail ── */}
      <aside className="rail">
        <div className="rail-head">
          <span className="dot" /><span>Bibi</span>
          <span className="version">v1.0 · live</span>
        </div>

        <div className="rail-orb">
          <BibiOrb size={170} state={phase === 'thinking' ? 'acting' : phase} style={orbStyle} />
          <Beeps trigger={beepTrigger} />
        </div>

        <div className="rail-title">
          {phase === 'idle' && "Hi, I'm Bibi"}
          {phase === 'listening' && "I'm listening…"}
          {phase === 'thinking' && 'On it!'}
          {phase === 'acting' && (workflow?.kind === 'question' ? 'Looking it up…' : 'Working…')}
          {phase === 'done' && (isQuestion ? 'Here you go' : 'All done!')}
        </div>

        <div className="rail-status">
          {phase !== 'idle'
            ? <span className="pill"><i /> {statusLabel}</span>
            : <span style={{ color: 'var(--ink-4)' }}>Say “Bibi” or pick a task</span>}
        </div>

        <div className={`rail-transcript ${!transcript && !statusBanner ? 'empty' : ''}`}>
          {transcript
            ? <span><span className="quote">“{transcript}”</span></span>
            : statusBanner
              ? <span>{statusBanner}</span>
              : (phase === 'idle' ? 'Say something, or tap a suggestion below' : ' ')}
        </div>

        {/* Idle suggestions (only before the first command) */}
        {!workflow && phase !== 'listening' && (
          <>
            <div className="rail-section-label">Try saying</div>
            <div className="chips">
              {SUGGESTIONS.map((s) => (
                <button key={s.text} className="chip" onClick={() => runText(s.text)}>
                  <span className="chip-glyph">{s.glyph}</span>
                  <span className="chip-text">“{s.text}”</span>
                  <span className="chip-arrow">→</span>
                </button>
              ))}
            </div>
          </>
        )}

        {/* Live plan */}
        {tasks.length > 0 && (
          <>
            <div className="rail-section-label">Plan</div>
            <div className="steps">
              {tasks.map((t, i) => (
                <div key={i} className={`step ${t.status === 'done' ? 'done' : t.status === 'active' ? 'active' : t.status === 'error' ? 'error' : ''}`}>
                  <span className="label">{t.title}</span>
                  {t.status === 'done' && <span className="check">✓</span>}
                  {t.status === 'error' && <span className="check" style={{ color: '#e63946' }}>!</span>}
                </div>
              ))}
            </div>
          </>
        )}

        {/* Always-available command box */}
        <form className="rail-typed" onSubmit={(e) => { e.preventDefault(); const c = typedCommand; setTypedCommand(''); void runText(c) }}>
          <input type="text" className="rail-typed-input" placeholder="Ask or tell Bibi anything…"
                 value={typedCommand} onChange={(e) => setTypedCommand(e.target.value)} autoComplete="off" />
          <button type="submit" className="rail-typed-send" disabled={!typedCommand.trim()} aria-label="Send">→</button>
        </form>

        {phase === 'listening' && (
          <>
            <div className="rail-section-label">Audio in</div>
            <div style={{ display: 'flex', justifyContent: 'center', gap: 3, padding: '16px 0', height: 56, alignItems: 'center' }}>
              {[...Array(36)].map((_, i) => (
                <i key={i} style={{ width: 2, background: 'var(--orange)', borderRadius: 1,
                  height: `${18 + Math.sin(i * 0.7) * 16 + Math.random() * 10}px`,
                  animation: `wave 0.8s ease-in-out ${i * 0.04}s infinite` }} />
              ))}
            </div>
          </>
        )}

        <button className={`wake-toggle ${wakeOn ? 'on' : ''}`} onClick={toggleWake}
                title={wakeOn ? 'Stop wake listening' : 'Listen for “Bibi”'}>
          <span className="wake-dot" />
          <span className="wake-label">{wakeOn ? (wakeStatus || 'Listening for “Bibi”…') : 'Enable “Bibi” wake word'}</span>
          <span className="wake-switch"><i /></span>
        </button>

        <div className="rail-foot">
          <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#5dd0c5' }} />
            Claude brain · real browser
          </span>
          <button className={`mic-btn ${isRecording ? 'recording' : ''}`} onClick={handleMic}
                  title="Tap and speak a command">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="3" width="6" height="12" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></svg>
          </button>
        </div>
      </aside>

      {/* ── Stage ── */}
      <div className="stage">
        <div className="stage-wallpaper" />
        <div style={{ position: 'relative', width: '100%', maxWidth: 1100, maxHeight: '100%', aspectRatio: '16/10', display: 'flex' }}>
          <Browser url={hasTasks ? 'controlling your screen' : 'newtab'}
                   tab={isQuestion ? 'Bibi' : hasTasks ? 'Bibi · your screen' : 'New Tab'}
                   idle={phase === 'idle' && !hasTasks && !isQuestion}>
            {isQuestion ? (
              <div className="answer-card">
                <div className="answer-orb"><BibiOrb size={120} state={phase === 'done' ? 'idle' : 'acting'} style={orbStyle} /></div>
                <div className="answer-text">{statusBanner || 'Thinking…'}</div>
                <div className="answer-sub">{phase === 'done' ? '● Spoken aloud' : 'Working on it…'}</div>
              </div>
            ) : hasTasks ? (
              <div className="activity-card">
                <div className="activity-hdr">
                  <span className="activity-dot" />
                  {phase === 'done' ? 'Done' : 'Working on your screen…'}
                </div>
                <div className="activity-list">
                  {tasks.map((t, i) => (
                    <div key={i} className={`activity-item ${t.status}`}>
                      <span className="activity-ico">
                        {t.status === 'done' ? '✓' : t.status === 'active' ? '◐' : t.status === 'error' ? '!' : '○'}
                      </span>
                      <span className="activity-title">{t.title}</span>
                      {t.detail && <span className="activity-detail">{t.detail}</span>}
                    </div>
                  ))}
                </div>
                <div className="activity-foot">{statusBanner}</div>
                {phase === 'done' && <button className="replay" onClick={reset}>← New command</button>}
              </div>
            ) : (
              <BrowserIdle />
            )}
          </Browser>
        </div>
      </div>
    </div>
  )
}

// ── WAV encoding (mic → 16k PCM WAV) ──────────────────────
function encodeWaveBlob(chunks, sr, target) {
  const merged = mergeChunks(chunks)
  const ds = downsample(merged, sr, target)
  const norm = normalize(ds)
  return new Blob([encodeWav(norm, target)], { type: 'audio/wav' })
}
function mergeChunks(chunks) {
  const len = chunks.reduce((s, c) => s + c.length, 0)
  const out = new Float32Array(len); let o = 0
  chunks.forEach((c) => { out.set(c, o); o += c.length }); return out
}
function downsample(buf, sr, target) {
  if (sr === target) return buf
  const ratio = sr / target, n = Math.max(1, Math.round(buf.length / ratio))
  const out = new Float32Array(n); let oi = 0, bi = 0
  while (oi < n) {
    const next = Math.min(buf.length, Math.round((oi + 1) * ratio))
    let sum = 0, cnt = 0
    for (let i = bi; i < next; i++) { sum += buf[i]; cnt++ }
    out[oi] = cnt ? sum / cnt : 0; oi++; bi = next
  }
  return out
}
function normalize(buf) {
  let peak = 0
  for (let i = 0; i < buf.length; i++) peak = Math.max(peak, Math.abs(buf[i]))
  if (peak <= 0) return buf
  const gain = Math.min(8, 0.92 / peak)
  if (gain <= 1.05) return buf
  const out = new Float32Array(buf.length)
  for (let i = 0; i < buf.length; i++) out[i] = Math.max(-1, Math.min(1, buf[i] * gain))
  return out
}
function encodeWav(samples, sr) {
  const bps = 2, buf = new ArrayBuffer(44 + samples.length * bps), v = new DataView(buf)
  const w = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)) }
  w(0, 'RIFF'); v.setUint32(4, 36 + samples.length * bps, true); w(8, 'WAVE'); w(12, 'fmt ')
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true)
  v.setUint32(24, sr, true); v.setUint32(28, sr * bps, true); v.setUint16(32, bps, true)
  v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, samples.length * bps, true)
  let off = 44
  for (let i = 0; i < samples.length; i++) { const s = Math.max(-1, Math.min(1, samples[i])); v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true); off += bps }
  return buf
}
