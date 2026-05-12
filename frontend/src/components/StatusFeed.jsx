import { useEffect, useRef } from 'react'
import './StatusFeed.css'

/**
 * StatusFeed — Scrollable log of the last 10 commands and their outcomes.
 *
 * Displays entries newest-at-bottom (terminal style) and auto-scrolls to the
 * latest entry whenever the commands array changes.
 *
 * Props
 * -----
 *  commands: array — log entries managed by App.
 *    Each entry shape:
 *      {
 *        id:        string,
 *        trigger:   string,
 *        intent:    object | null,
 *        result:    object | null,
 *        error:     string | null,
 *        timestamp: string,    ISO-8601
 *        confirmed: bool,      true after POST /confirm succeeded
 *        cancelled: bool       true when user dismissed the confirm modal
 *      }
 */
export default function StatusFeed({
  commands,
  title = 'Command Log',
  emptyText = 'No commands yet. Tap to Speak to start.',
}) {
  const bottomRef = useRef(null)

  // Scroll to newest entry whenever the list changes
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [commands])

  return (
    <section className="status-feed" aria-label="Command history">
      <div className="status-feed__header">
        <div>
          <span className="status-feed__eyebrow">Session Thread</span>
          <h2 className="status-feed__heading">{title}</h2>
        </div>
        <span className="status-feed__count">
          {commands.length} event{commands.length === 1 ? '' : 's'}
        </span>
      </div>

      <div className="status-feed__body">
        {commands.length === 0 ? (
          <p className="status-feed__empty">
            {emptyText}
          </p>
        ) : (
          <ol className="status-feed__list" aria-label="Command entries">
            {commands.map((entry) => (
              <CommandEntry key={entry.id} entry={entry} />
            ))}
          </ol>
        )}
        <div ref={bottomRef} aria-hidden="true" />
      </div>
    </section>
  )
}

/**
 * CommandEntry — card for a single command/result pair.
 *
 * @param {{ entry: object }} props
 */
function CommandEntry({ entry }) {
  const { intent, result, error, timestamp, confirmed, cancelled } = entry

  const hasError   = Boolean(error)
  const intentName = intent?.intent    ?? 'unknown'
  const transcript = intent?.raw_transcript ?? null
  const message    = result?.message   ?? null
  const success    = result?.success   ?? false

  // Determine the visual status of this entry
  let status = 'pending'
  if (cancelled)                 status = 'cancelled'
  else if (hasError)             status = 'error'
  else if (confirmed)            status = 'confirmed'
  else if (success)              status = 'success'
  else if (result !== null)      status = 'warning'

  const badges = {
    success:   'Success',
    confirmed: 'Confirmed',
    error:     'Error',
    warning:   'Warning',
    cancelled: 'Cancelled',
    pending:   'Pending',
  }

  const time = new Date(timestamp).toLocaleTimeString([], {
    hour:   '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })

  return (
    <li className={`cmd-card cmd-card--${status}`}>
      <div className="cmd-card__header">
        <div className="cmd-card__title-group">
          <span className={`cmd-card__badge cmd-card__badge--${status}`}>
            {badges[status]}
          </span>
          <span className="cmd-card__intent">{formatIntent(intentName)}</span>
        </div>
        <time className="cmd-card__time" dateTime={timestamp}>{time}</time>
      </div>

      {transcript && (
        <p className="cmd-card__row">
          <span className="cmd-card__label">Heard</span>
          <span className="cmd-card__value">&ldquo;{transcript}&rdquo;</span>
        </p>
      )}

      {message && !cancelled && (
        <p className="cmd-card__row">
          <span className="cmd-card__label">Result</span>
          <span className="cmd-card__value">{message}</span>
        </p>
      )}

      {hasError && (
        <p className="cmd-card__row cmd-card__row--error">
          <span className="cmd-card__label">Error</span>
          <span className="cmd-card__value">{error}</span>
        </p>
      )}

      {cancelled && (
        <p className="cmd-card__row cmd-card__row--muted">
          <span className="cmd-card__value">Action was cancelled.</span>
        </p>
      )}

      {result?.data?.requires_clarification && (
        <p className="cmd-card__clarify">
          {result.data.follow_up}
        </p>
      )}

      {result?.data?.requires_confirmation && !confirmed && !cancelled && (
        <p className="cmd-card__row cmd-card__row--muted">
          <span className="cmd-card__value">Waiting for confirmation&hellip;</span>
        </p>
      )}
    </li>
  )
}

/**
 * Convert snake_case intent name to Title Case label.
 * @param {string} intent
 * @returns {string}
 */
function formatIntent(intent) {
  return intent
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}
