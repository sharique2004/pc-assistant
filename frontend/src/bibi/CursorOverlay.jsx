// Floating cursor + bounding-box overlay — paints on top of the mock browser
// while a scenario runs.

const CursorSVG = () => (
  <svg viewBox="0 0 28 28">
    <defs>
      <filter id="cursor-glow" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="1.5" result="b" />
        <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
      </filter>
    </defs>
    <g filter="url(#cursor-glow)">
      <path d="M3 2 L3 21 L8.5 16 L11.5 23 L14 22 L11 15 L18.5 15 Z"
            fill="#fff" stroke="#1a1a1a" strokeWidth="1" strokeLinejoin="round" />
      <path d="M5 5 L5 17 L8 14 L11.5 21 L12.5 20.5 L9 13.5 L14 13.5 Z"
            fill="#ff6a1a" />
    </g>
  </svg>
)

export default function CursorOverlay({ cursor, bbox }) {
  return (
    <div className="bibi-overlay">
      {bbox && (
        <div className="bibi-bbox" style={{
          left: bbox.x - 4, top: bbox.y - 4,
          width: bbox.w + 8, height: bbox.h + 8,
        }}>
          <span className="c1" /><span className="c2" />
          {bbox.label && <div className="bibi-bbox-label">{bbox.label}</div>}
        </div>
      )}
      {cursor.visible && (
        <div className={`bibi-cursor ${cursor.clicking ? 'clicking' : ''}`}
             style={{ left: cursor.x, top: cursor.y }}>
          <CursorSVG />
          {cursor.label && <div className="bibi-cursor-label">{cursor.label}</div>}
        </div>
      )}
    </div>
  )
}
