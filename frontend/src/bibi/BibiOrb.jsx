// Bibi orb — visual heart of the assistant. Four styles, four states.
// States: idle | listening | acting | done
// Styles: rings | core | crosshair | droid

export default function BibiOrb({ size = 200, state = 'idle', style = 'rings' }) {
  const renderRings = () => (
    <svg width={size} height={size} viewBox="-100 -100 200 200">
      <g className="breath">
        <circle className="bibi-pulse-ring" r="42" />
        <circle className="bibi-pulse-ring p2" r="42" />
        <circle className="bibi-pulse-ring p3" r="42" />

        <circle className="bibi-rings-outer" r="78" strokeDasharray="2 4" />
        <circle className="bibi-rings-outer" r="64" strokeDasharray="1 6" opacity="0.3" />

        <circle className="bibi-rings-mid" r="44" strokeDasharray="80 8 12 8" />

        <circle className="bibi-rings-core" r="14" />

        <circle className="bibi-rings-head" cx="18" cy="-12" r="3.2" />

        {[0, 90, 180, 270].map((deg) => (
          <line key={deg}
            x1={Math.cos((deg - 90) * Math.PI / 180) * 84}
            y1={Math.sin((deg - 90) * Math.PI / 180) * 84}
            x2={Math.cos((deg - 90) * Math.PI / 180) * 92}
            y2={Math.sin((deg - 90) * Math.PI / 180) * 92}
            stroke="var(--o)" strokeWidth="1.5" opacity="0.6"
          />
        ))}
      </g>
    </svg>
  )

  const renderCore = () => (
    <svg width={size} height={size} viewBox="-100 -100 200 200">
      <defs>
        <radialGradient id="bibi-core-grad" cx="40%" cy="35%">
          <stop offset="0%" stopColor="#ffd9b0" />
          <stop offset="30%" stopColor="#ffa64d" />
          <stop offset="70%" stopColor="#ff6a1a" />
          <stop offset="100%" stopColor="#7a2a05" />
        </radialGradient>
      </defs>
      <g className="breath">
        <circle className="bibi-pulse-ring" r="46" />
        <circle className="bibi-pulse-ring p2" r="46" />
        <circle className="bibi-pulse-ring p3" r="46" />
        <circle className="bibi-core-glow" r="46" fill="url(#bibi-core-grad)" />
        <ellipse cx="-14" cy="-22" rx="14" ry="9" fill="#fff" opacity="0.35" />
        <ellipse cx="-18" cy="-26" rx="5" ry="3" fill="#fff" opacity="0.6" />
      </g>
    </svg>
  )

  const renderCrosshair = () => (
    <svg width={size} height={size} viewBox="-100 -100 200 200">
      <g className="breath">
        <circle className="bibi-pulse-ring" r="44" />
        <circle className="bibi-pulse-ring p2" r="44" />
        <line className="bibi-cross-line" x1="-90" y1="0" x2="-30" y2="0" />
        <line className="bibi-cross-line" x1="30" y1="0" x2="90" y2="0" />
        <line className="bibi-cross-line" x1="0" y1="-90" x2="0" y2="-30" />
        <line className="bibi-cross-line" x1="0" y1="30" x2="0" y2="90" />
        {[[-1,-1],[1,-1],[-1,1],[1,1]].map(([sx, sy], i) => (
          <path key={i}
            d={`M ${sx*48} ${sy*48-sy*8} L ${sx*48} ${sy*48} L ${sx*48-sx*8} ${sy*48}`}
            stroke="var(--o)" strokeWidth="1.5" fill="none" opacity="0.7" />
        ))}
        <circle className="bibi-cross-ring" r="28" />
        <circle className="bibi-cross-dot" r="6" />
        <circle cx="14" cy="-10" r="2.2" fill="#f4eee7" opacity="0.9" />
      </g>
    </svg>
  )

  const renderDroid = () => (
    <svg width={size} height={size} viewBox="-100 -100 200 200">
      <g className="breath">
        <circle className="bibi-pulse-ring" r="58" />
        <circle className="bibi-pulse-ring p2" r="58" />
        <circle className="bibi-droid-body" cx="0" cy="20" r="52" />
        <circle className="bibi-droid-stripe" cx="-22" cy="8" r="8" />
        <circle className="bibi-droid-stripe" cx="20" cy="36" r="6" />
        <circle className="bibi-droid-stripe" cx="-8" cy="48" r="4" />
        <path d="M -52 20 Q 0 30 52 20" stroke="rgba(0,0,0,0.1)" strokeWidth="0.5" fill="none" />
        <path d="M -50 36 Q 0 46 50 36" stroke="rgba(0,0,0,0.08)" strokeWidth="0.5" fill="none" />
        <ellipse className="bibi-droid-head" cx="0" cy="-30" rx="34" ry="22" />
        <path d="M -34 -30 Q 0 -56 34 -30" stroke="rgba(0,0,0,0.1)" strokeWidth="0.5" fill="none" />
        <rect x="-34" y="-30" width="68" height="3" fill="var(--o)" opacity="0.85" />
        <circle className="bibi-droid-eye" cx="-6" cy="-36" r="6" />
        <circle className="bibi-droid-eye-shine" cx="-4" cy="-38" r="1.6" />
        <circle className="bibi-droid-eye" cx="12" cy="-34" r="2.4" />
        <line x1="14" y1="-50" x2="18" y2="-62" stroke="#f4eee7" strokeWidth="1" />
        <circle cx="18" cy="-62" r="1.6" fill="var(--o)" />
      </g>
    </svg>
  )

  const renderers = { rings: renderRings, core: renderCore, crosshair: renderCrosshair, droid: renderDroid }
  const render = renderers[style] || renderRings

  const showWave = state === 'listening' && style !== 'droid'

  return (
    <div className={`bibi-orb bibi-${state} ${style === 'droid' ? 'no-wave' : ''}`}
         style={{ width: size, height: size }}>
      {render()}
      {showWave && (
        <div className="bibi-waveform">
          <i /><i /><i /><i /><i />
        </div>
      )}
    </div>
  )
}
