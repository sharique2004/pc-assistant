import { useEffect, useCallback } from 'react'
import './ConfirmModal.css'

/**
 * ConfirmModal — dialog shown when an executor operation requires explicit
 * user approval before it runs (requires_confirmation: true).
 *
 * Rendered by App.jsx when pendingConfirm is non-null.
 * Closes on Confirm, Cancel, Escape key, or clicking the backdrop.
 *
 * Props
 * -----
 *  description (string) — human-readable description of the pending action,
 *                         e.g. "Create notes.txt in C:/Users/Me/Documents"
 *  onConfirm   (fn)     — called when user clicks Confirm
 *  onCancel    (fn)     — called when user clicks Cancel / presses Escape
 */
export default function ConfirmModal({ description, onConfirm, onCancel }) {
  // Close on Escape key
  const handleKeyDown = useCallback(
    (e) => {
      if (e.key === 'Escape') onCancel()
    },
    [onCancel],
  )

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [handleKeyDown])

  // Close when clicking outside the modal box
  const handleBackdropClick = useCallback(
    (e) => {
      if (e.target === e.currentTarget) onCancel()
    },
    [onCancel],
  )

  return (
    <div
      className="modal-backdrop"
      onClick={handleBackdropClick}
      role="dialog"
      aria-modal="true"
      aria-labelledby="modal-title"
      aria-describedby="modal-desc"
    >
      <div className="modal">
        <div className="modal__icon" aria-hidden="true">&#9888;</div>

        <h2 className="modal__title" id="modal-title">
          Confirm Action
        </h2>

        <p className="modal__desc" id="modal-desc">
          {description}
        </p>

        <div className="modal__actions">
          <button
            className="modal__btn modal__btn--cancel"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            className="modal__btn modal__btn--confirm"
            onClick={onConfirm}
            autoFocus
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  )
}
