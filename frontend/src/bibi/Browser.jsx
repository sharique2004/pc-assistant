// Bibi-themed mock browser chrome — the inner "page" is the scenario screen.

export default function Browser({ url, tab, children, idle }) {
  return (
    <div className={`browser-wrap ${idle ? 'idle' : ''}`}>
      <div style={{
        width: '100%', height: '100%',
        borderRadius: 12, overflow: 'hidden',
        boxShadow: '0 30px 80px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.06)',
        display: 'flex', flexDirection: 'column',
        background: '#1d1d20',
      }}>
        <div style={{
          height: 36, background: '#26262b',
          display: 'flex', alignItems: 'center',
          padding: '0 14px', gap: 10,
          borderBottom: '1px solid rgba(255,255,255,0.04)',
        }}>
          <div style={{ display: 'flex', gap: 7 }}>
            <i style={{ width: 11, height: 11, borderRadius: '50%', background: '#ff5f57' }} />
            <i style={{ width: 11, height: 11, borderRadius: '50%', background: '#febc2e' }} />
            <i style={{ width: 11, height: 11, borderRadius: '50%', background: '#28c840' }} />
          </div>

          <div style={{
            marginLeft: 14, height: 26,
            background: '#37373e',
            borderRadius: '8px 8px 0 0',
            padding: '0 12px',
            display: 'flex', alignItems: 'center', gap: 8,
            fontSize: 12, color: '#e8e8ed',
            minWidth: 180, maxWidth: 260,
            transform: 'translateY(5px)',
          }}>
            <i style={{ width: 12, height: 12, borderRadius: 3, background: '#ff6a1a',
              flexShrink: 0, opacity: 0.85 }} />
            <span style={{ flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {tab}
            </span>
          </div>

          <div style={{ flex: 1 }} />
          <div style={{ display: 'flex', gap: 14, color: '#8e8e93', fontSize: 12 }}>
            <span>⌘</span><span>⋯</span>
          </div>
        </div>

        <div style={{
          height: 40, background: '#37373e',
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '0 10px',
        }}>
          <div style={{ display: 'flex', gap: 6, color: '#8e8e93' }}>
            <span style={{ width: 22, height: 22, display: 'flex', alignItems: 'center', justifyContent: 'center', borderRadius: 4 }}>‹</span>
            <span style={{ width: 22, height: 22, display: 'flex', alignItems: 'center', justifyContent: 'center', borderRadius: 4, opacity: 0.4 }}>›</span>
            <span style={{ width: 22, height: 22, display: 'flex', alignItems: 'center', justifyContent: 'center', borderRadius: 4 }}>↻</span>
          </div>
          <div style={{
            flex: 1, height: 28, background: '#26262b', borderRadius: 6,
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '0 12px',
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: 12, color: '#c9c4ba',
          }}>
            <span style={{ color: '#8e8e93' }}>🔒</span>
            <span>{url || 'about:blank'}</span>
            {url && (
              <span style={{ marginLeft: 'auto', color: '#ff6a1a', fontSize: 10,
                fontWeight: 600, letterSpacing: 0.5, textTransform: 'uppercase' }}>
                Bibi · Controlled
              </span>
            )}
          </div>
          <div style={{ width: 22, height: 22, color: '#8e8e93', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>⋯</div>
        </div>

        <div style={{ flex: 1, position: 'relative', overflow: 'hidden', background: '#fff' }}>
          {children}
        </div>
      </div>
    </div>
  )
}

export function BrowserIdle() {
  return (
    <div style={{
      width: '100%', height: '100%',
      background: 'linear-gradient(180deg, #1d1d20 0%, #0d0d12 100%)',
      display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center',
      gap: 18,
    }}>
      <div style={{ color: 'rgba(255,255,255,0.35)', fontFamily: "'JetBrains Mono', monospace",
        fontSize: 11, letterSpacing: 0.15, textTransform: 'uppercase' }}>
        Bibi standing by
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{ width: 6, height: 6, borderRadius: '50%', background: '#ff6a1a',
          boxShadow: '0 0 12px #ff6a1a', animation: 'pulse-dot 2s ease-in-out infinite' }} />
        <span style={{ color: 'rgba(255,255,255,0.55)', fontSize: 13 }}>
          Pick a task from the left — or hit the mic
        </span>
      </div>
    </div>
  )
}
