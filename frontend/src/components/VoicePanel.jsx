import './VoicePanel.css'

export default function VoicePanel({
  speechSupported,
  voiceEnabled,
  isSpeaking,
  voices,
  selectedVoiceURI,
  speechRate,
  onToggleEnabled,
  onSelectVoice,
  onRateChange,
  onStopSpeaking,
  onTestVoice,
}) {
  if (!speechSupported) {
    return (
      <section className="voice-panel" aria-label="Voice replies">
        <div className="voice-panel__header">
          <h2 className="voice-panel__title">Voice Replies</h2>
          <span className="voice-panel__status voice-panel__status--muted">Unavailable</span>
        </div>
        <p className="voice-panel__hint">
          This browser does not expose local speech synthesis, so spoken replies are unavailable here.
        </p>
      </section>
    )
  }

  return (
    <section className="voice-panel" aria-label="Voice replies">
      <div className="voice-panel__header">
        <div>
          <h2 className="voice-panel__title">Voice Replies</h2>
          <p className="voice-panel__hint">
            Uses your local Windows voice engine through the backend so replies stay offline.
          </p>
        </div>
        <span className={`voice-panel__status ${isSpeaking ? 'voice-panel__status--speaking' : ''}`}>
          {isSpeaking ? 'Speaking' : voiceEnabled ? 'Ready' : 'Muted'}
        </span>
      </div>

      <div className="voice-panel__controls">
        <label className="voice-panel__field">
          <span className="voice-panel__label">Replies</span>
          <button
            type="button"
            className={`voice-panel__toggle ${voiceEnabled ? 'voice-panel__toggle--on' : ''}`}
            onClick={onToggleEnabled}
            aria-pressed={voiceEnabled}
          >
            {voiceEnabled ? 'On' : 'Off'}
          </button>
        </label>

        <label className="voice-panel__field">
          <span className="voice-panel__label">Voice</span>
          <select
            className="voice-panel__select"
            value={selectedVoiceURI}
            onChange={(event) => onSelectVoice(event.target.value)}
            disabled={!voiceEnabled || voices.length <= 1}
          >
            <option value="">
              {voices.length ? 'System default voice' : 'Loading installed voices...'}
            </option>
            {voices.map((voice) => (
              <option key={voice.voiceURI} value={voice.voiceURI}>
                {voice.name} ({voice.lang})
              </option>
            ))}
          </select>
        </label>

        <label className="voice-panel__field">
          <span className="voice-panel__label">Speed</span>
          <input
            className="voice-panel__range"
            type="range"
            min="0.75"
            max="1.25"
            step="0.01"
            value={speechRate}
            onChange={(event) => onRateChange(Number(event.target.value))}
            disabled={!voiceEnabled}
          />
          <span className="voice-panel__value">{speechRate.toFixed(2)}x</span>
        </label>
      </div>

      <div className="voice-panel__actions">
        <button
          type="button"
          className="voice-panel__button"
          onClick={onTestVoice}
          disabled={!voiceEnabled}
        >
          Test Voice
        </button>
        <button
          type="button"
          className="voice-panel__button voice-panel__button--secondary"
          onClick={onStopSpeaking}
          disabled={!isSpeaking}
        >
          Stop
        </button>
      </div>
    </section>
  )
}
