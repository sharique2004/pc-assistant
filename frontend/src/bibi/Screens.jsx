// Mock browser screens. Each reads from `s` (screenState) — scenario steps
// mutate it via doneState/preState merges in BibiApp.

const Icon = {
  search: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>,
  menu: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></svg>,
  msg: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>,
  send: <svg viewBox="0 0 24 24" fill="currentColor"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg>,
  plus: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 5v14M5 12h14"/></svg>,
  arrow: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M5 12h14M13 5l7 7-7 7"/></svg>,
  file: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>,
  folder: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>,
  chev: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m6 9 6 6 6-6"/></svg>,
}

const Caret = () => <span className="typing-caret" />

const WA_CHATS = [
  { id: 'mom',     name: 'Mom',           color: '#e91e63', preview: 'Don\'t forget dinner Sunday ♥', time: '14:22', unread: 0 },
  { id: 'sam',     name: 'Sam',           color: '#1e88e5', preview: 'See you tomorrow!',              time: '13:01', unread: 2 },
  { id: 'work',    name: 'Design Team',   color: '#7b1fa2', preview: 'Maya: pushing v2 today',         time: '12:44', unread: 0 },
  { id: 'priya',   name: 'Priya',         color: '#43a047', preview: 'lol fair point',                 time: '11:08', unread: 0 },
  { id: 'gym',     name: 'Yousef ⚡',      color: '#fb8c00', preview: '6am tomorrow?',                  time: 'Yesterday', unread: 1 },
  { id: 'fam',     name: 'Family',        color: '#5d4037', preview: 'Dad: I sent the photos',         time: 'Yesterday', unread: 0 },
  { id: 'jules',   name: 'Jules',         color: '#00897b', preview: 'no worries we can reschedule',   time: 'Mon', unread: 0 },
]

const WA_PRIOR = [
  { side: 'in', text: 'how was the trip??', time: '12:54' },
  { side: 'out', text: 'magic. i\'ll send pics later', time: '12:58' },
  { side: 'in', text: 'pls do', time: '13:00' },
  { side: 'in', text: 'also are you free this week', time: '13:01' },
]

export function WhatsAppScreen({ s }) {
  const filter = (s.searchQuery || '').toLowerCase()
  const chats = filter ? WA_CHATS.filter((c) => c.name.toLowerCase().includes(filter)) : WA_CHATS
  const sel = s.selectedChat
  const chat = WA_CHATS.find((c) => c.id === sel)

  return (
    <div className="scr scr-wa">
      <div className="scr-wa-bg" />
      <aside className="wa-side">
        <div className="wa-side-hdr">
          <div className="wa-avatar">U</div>
          <div className="wa-side-icons">
            <span>{Icon.msg}</span>
            <span>{Icon.menu}</span>
          </div>
        </div>
        <div className="wa-search">
          <div className={`wa-search-input ${s.searchFocused ? 'focused' : ''}`}
               data-bibi-target="wa-search">
            {Icon.search}
            <span style={{ color: s.searchQuery ? '#111b21' : undefined }}>
              {s.searchQuery || 'Search or start a new chat'}
              {s.searchFocused && <Caret />}
            </span>
          </div>
        </div>
        <div className="wa-chats">
          {chats.map((c) => (
            <div key={c.id}
                 className={`wa-chat ${sel === c.id ? 'active' : ''}`}
                 data-bibi-target={`wa-chat-${c.id}`}>
              <div className="wa-chat-av" style={{ background: c.color }}>
                {c.name[0]}
              </div>
              <div>
                <div className="wa-chat-name">{c.name}</div>
                <div className="wa-chat-preview">{c.preview}</div>
              </div>
              <div className="wa-chat-meta">
                <div>{c.time}</div>
                {c.unread > 0 && <span className="wa-chat-badge">{c.unread}</span>}
              </div>
            </div>
          ))}
          {chats.length === 0 && (
            <div style={{ padding: 30, color: '#667781', textAlign: 'center', fontSize: 13 }}>
              No chats match &quot;{s.searchQuery}&quot;
            </div>
          )}
        </div>
      </aside>

      {!chat ? (
        <div className="wa-empty">
          <div className="badge" />
          <h2>WhatsApp Web</h2>
          <p>Send and receive messages without keeping your phone online.</p>
        </div>
      ) : (
        <main className="wa-main">
          <div className="wa-main-hdr">
            <div className="wa-chat-av" style={{ width: 36, height: 36, background: chat.color }}>
              {chat.name[0]}
            </div>
            <div>
              <div className="name">{chat.name}</div>
              <div className="status">online</div>
            </div>
          </div>
          <div className="wa-msgs">
            {WA_PRIOR.map((m, i) => (
              <div key={i} className={`wa-msg ${m.side === 'in' ? 'in' : ''}`}>
                {m.text} <span className="time">{m.time}</span>
              </div>
            ))}
            {s.sentMessage && (
              <div className="wa-msg" style={{ animation: 'pop .3s cubic-bezier(.5,1.6,.5,1)' }}>
                {s.sentMessage} <span className="time">just now ✓✓</span>
              </div>
            )}
          </div>
          <div className="wa-compose">
            <div className={`wa-compose-input ${s.composeFocused ? 'focused' : ''} ${!s.draft ? 'empty' : ''}`}
                 data-bibi-target="wa-compose">
              {s.draft || 'Type a message'}
              {s.composeFocused && <Caret />}
            </div>
            <div className="wa-send" data-bibi-target="wa-send">{Icon.send}</div>
          </div>
        </main>
      )}
    </div>
  )
}

const FL_RESULTS = [
  { airline: 'Emirates',     code: 'EK', logo: '#d71920', dep: '02:35', arr: '13:45+1', dur: '7h 10m', stops: 'Nonstop', price: '$842' },
  { airline: 'Qatar Airways',code: 'QR', logo: '#5c0632', dep: '09:10', arr: '22:55',   dur: '9h 45m', stops: '1 stop · DOH', price: '$691' },
  { airline: 'British Airways',code:'BA',logo: '#075aaa', dep: '14:20', arr: '07:05+1', dur: '8h 45m', stops: 'Nonstop', price: '$978' },
]

export function FlightsScreen({ s }) {
  return (
    <div className="scr scr-fl">
      <div className="fl-hdr">
        <div className="fl-logo">
          <span className="g1">G</span><span className="g2">o</span><span className="g3">o</span>
          <span className="g4">g</span><span className="g5">l</span><span className="g6">e</span>
          {' '}<span style={{ color: '#202124', fontWeight: 500 }}>Flights</span>
        </div>
        <nav className="fl-nav">
          <span>Travel</span>
          <span>Explore</span>
          <span className="active">Flights</span>
          <span>Hotels</span>
          <span>Vacation rentals</span>
        </nav>
      </div>

      {!s.searched ? (
        <div className="fl-hero">
          <h1>Flights</h1>
          <div style={{ marginBottom: 10 }}>
            <div className="fl-pill" style={{ display: 'inline-flex', marginRight: 8 }}>Round trip {Icon.chev}</div>
            <div className="fl-pill" style={{ display: 'inline-flex', marginRight: 8 }}>1 passenger {Icon.chev}</div>
            <div className="fl-pill" style={{ display: 'inline-flex' }}>Economy {Icon.chev}</div>
          </div>
          <div className="fl-form" style={{ position: 'relative' }}>
            <div className="fl-field">
              <span className="lbl">From</span>
              <span className="val">San Francisco (SFO)</span>
            </div>
            <div className={`fl-field ${s.destFocused ? 'focused' : ''}`} data-bibi-target="fl-dest"
                 style={{ position: 'relative' }}>
              <span className="lbl">Where to?</span>
              <span className={`val ${!s.destQuery ? 'placeholder' : ''}`}>
                {s.destQuery || 'Anywhere'}
                {s.destFocused && <Caret />}
              </span>
              {s.destFocused && s.destQuery && (
                <div className="fl-suggest" style={{ left: -1, right: -1, top: '100%' }}>
                  <div className={`fl-suggest-item ${s.suggestHighlight ? 'hl' : ''}`} data-bibi-target="fl-suggest-dxb">
                    <div style={{ width: 28, height: 28, borderRadius: 4, background: '#e8f0fe',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      color: '#1967d2', fontSize: 13 }}>✈</div>
                    <div>
                      <div style={{ color: '#202124', fontSize: 14 }}>Dubai</div>
                      <div style={{ color: '#5f6368', fontSize: 12 }}>United Arab Emirates</div>
                    </div>
                    <span className="iata">DXB</span>
                  </div>
                  <div className="fl-suggest-item">
                    <div style={{ width: 28, height: 28, borderRadius: 4, background: '#fce8e6',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      color: '#c5221f', fontSize: 13 }}>✈</div>
                    <div>
                      <div style={{ color: '#202124', fontSize: 14 }}>Dublin</div>
                      <div style={{ color: '#5f6368', fontSize: 12 }}>Ireland</div>
                    </div>
                    <span className="iata">DUB</span>
                  </div>
                </div>
              )}
            </div>
            <div className="fl-field">
              <span className="lbl">Depart</span>
              <span className="val">Fri, May 30</span>
            </div>
            <div className="fl-field">
              <span className="lbl">Return</span>
              <span className="val">Sun, Jun 8</span>
            </div>
            <div className="fl-search-btn" data-bibi-target="fl-search">
              {Icon.search} Search
            </div>
          </div>
        </div>
      ) : (
        <div className="fl-results">
          <h3>Best departing flights · SFO → DXB</h3>
          {FL_RESULTS.map((r, i) => (
            <div key={i} className={`fl-result ${s.selectedFlight === i ? 'hl' : ''}`}
                 data-bibi-target={`fl-result-${i}`}>
              <div className="fl-result-logo" style={{ background: r.logo, display: 'flex',
                alignItems: 'center', justifyContent: 'center', color: '#fff', fontSize: 11, fontWeight: 700 }}>
                {r.code}
              </div>
              <div>
                <div className="fl-result-times">{r.dep} — {r.arr}</div>
                <div className="fl-result-airline">{r.airline}</div>
              </div>
              <div>
                <div className="fl-result-dur">{r.dur}</div>
                <div className="fl-result-stops">SFO–DXB</div>
              </div>
              <div className="fl-result-stops">{r.stops}</div>
              <div>
                <div className="fl-result-price">{r.price}{r.price.includes('$') && <small>round trip</small>}</div>
              </div>
            </div>
          ))}
          {s.bookingConfirmed && (
            <div style={{ marginTop: 24, padding: 20, background: '#e6f4ea', borderRadius: 12,
              border: '1px solid #34a853', color: '#137333' }}>
              ✓ Holding seat 14A on Emirates EK225 — confirm payment to book.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function ChatGPTScreen({ s }) {
  return (
    <div className="scr scr-gpt">
      <aside className="gpt-side">
        <div className="gpt-side-btn">{Icon.plus} New chat</div>
        <div className="gpt-side-btn">{Icon.search} Search chats</div>
        <div className="gpt-side-divider">Recent</div>
        <div className="gpt-side-chat">Postgres migration plan</div>
        <div className="gpt-side-chat">Trip ideas — Lisbon</div>
        <div className="gpt-side-chat">Refactor auth layer</div>
        <div className="gpt-side-chat">Tax deductions for...</div>
        <div className="gpt-side-chat">Why is the sky blue</div>
      </aside>
      <main className="gpt-main">
        <div className="gpt-logo">
          <svg viewBox="0 0 24 24" fill="currentColor">
            <path d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.91 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.985 5.985 0 0 0-3.998 2.9 6.046 6.046 0 0 0 .743 7.097 5.98 5.98 0 0 0 .51 4.911 6.051 6.051 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.997-2.9 6.056 6.056 0 0 0-.747-7.073zM13.26 22.43a4.476 4.476 0 0 1-2.876-1.04l.141-.081 4.779-2.758a.795.795 0 0 0 .392-.681v-6.737l2.02 1.168a.071.071 0 0 1 .038.052v5.583a4.504 4.504 0 0 1-4.494 4.494zM3.6 18.304a4.47 4.47 0 0 1-.535-3.014l.142.085 4.783 2.759a.771.771 0 0 0 .78 0l5.843-3.369v2.332a.08.08 0 0 1-.033.062L9.74 19.95a4.5 4.5 0 0 1-6.14-1.646zM2.34 7.896a4.485 4.485 0 0 1 2.366-1.973V11.6a.766.766 0 0 0 .388.676l5.815 3.355-2.02 1.168a.076.076 0 0 1-.071 0l-4.83-2.786A4.504 4.504 0 0 1 2.34 7.872zm16.597 3.855-5.833-3.387L15.119 7.2a.076.076 0 0 1 .071 0l4.83 2.791a4.494 4.494 0 0 1-.676 8.105v-5.678a.79.79 0 0 0-.407-.667zm2.01-3.023-.141-.085-4.774-2.782a.776.776 0 0 0-.785 0L9.409 9.23V6.897a.066.066 0 0 1 .028-.061l4.83-2.787a4.5 4.5 0 0 1 6.68 4.66zm-12.64 4.135-2.02-1.164a.08.08 0 0 1-.038-.057V6.075a4.5 4.5 0 0 1 7.375-3.453l-.142.08L8.704 5.46a.795.795 0 0 0-.393.681zm1.097-2.365 2.602-1.5 2.607 1.5v2.999l-2.597 1.5-2.607-1.5z"/>
          </svg>
        </div>
        <div className="gpt-greeting">What can I help with?</div>
        <div className="gpt-input-wrap">
          <div className={`gpt-input ${!s.prompt ? 'empty' : ''}`} data-bibi-target="gpt-input">
            {s.prompt || 'Ask anything'}
            {s.promptFocused && <Caret />}
          </div>
          <div className="gpt-input-send" data-bibi-target="gpt-send">{Icon.arrow}</div>
        </div>
        <div className="gpt-suggests">
          <span className="gpt-suggest">Summarize text</span>
          <span className="gpt-suggest">Brainstorm</span>
          <span className="gpt-suggest">Make a plan</span>
          <span className="gpt-suggest">Help me write</span>
        </div>
      </main>
    </div>
  )
}

const CD_CODE_LINES = [
  { kw: 'export', sp: ' ', kw2: 'default', sp2: ' ', kw3: 'function', sp3: ' ', fn: 'CaloriePage', pun: '() {' },
  { ind: 1, kw: 'const', sp: ' ', pun: '[', name: 'eaten', sp2: ', ', name2: 'setEaten', pun2: '] = ', fn: 'useState', pun3: '(', num: '1240', pun4: ');' },
  { ind: 1, kw: 'const', sp: ' ', name: 'goal', sp2: ' = ', num: '2000', pun: ';' },
  { ind: 1, kw: 'const', sp: ' ', name: 'pct', sp2: ' = ', name2: 'Math', pun: '.', fn: 'round', pun2: '(', name3: 'eaten', pun3: ' / ', name4: 'goal', pun4: ' * ', num: '100', pun5: ');' },
  { blank: true },
  { ind: 1, kw: 'return', sp: ' ', pun: '(' },
  { ind: 2, jsx: '<div', attr: ' className', pun: '=', str: '"app"', jsx2: '>' },
  { ind: 3, jsx: '<h1>', str: 'Calories today', jsx2: '</h1>' },
  { ind: 3, jsx: '<Ring', attr: ' value', pun: '=', pun2: '{', name: 'pct', pun3: '}', attr2: ' label', pun4: '=', str: '{`${eaten}/${goal}`}', jsx2: ' />' },
  { ind: 3, jsx: '<MealList', attr: ' meals', pun: '=', pun2: '{', name: 'todaysMeals', pun3: '}', jsx2: ' />' },
  { ind: 3, jsx: '<button', attr: ' onClick', pun: '=', pun2: '{', name: 'addMeal', pun3: '}', jsx2: '>Add meal</button>' },
  { ind: 2, jsx: '</div>' },
  { ind: 1, pun: ');' },
  { pun: '}' },
]

const CD_FILES = [
  { id: 'page', name: 'CaloriePage.tsx', step: 0 },
  { id: 'ring', name: 'Ring.tsx', step: 1 },
  { id: 'meals', name: 'MealList.tsx', step: 2 },
  { id: 'data', name: 'meals.ts', step: 2 },
  { id: 'pkg', name: 'package.json', step: 0 },
]

function CodeLine({ tok }) {
  if (tok.blank) return <div className="cd-code-line"><span className="cd-ln-no" /><span /></div>
  const order = ['ind','kw','sp','kw2','sp2','kw3','sp3','fn','pun','name','name2','pun2','fn2','pun3','name3','num','pun4','name4','pun5','jsx','attr','attr2','str','jsx2']
  const cls = {
    kw: 'cd-kw', kw2: 'cd-kw', kw3: 'cd-kw',
    fn: 'cd-fn', fn2: 'cd-fn',
    str: 'cd-str',
    num: 'cd-num',
    pun: 'cd-pun', pun2: 'cd-pun', pun3: 'cd-pun', pun4: 'cd-pun', pun5: 'cd-pun',
    name: 'cd-type', name2: 'cd-type', name3: 'cd-type', name4: 'cd-type',
    jsx: 'cd-jsx', jsx2: 'cd-jsx',
    attr: 'cd-attr', attr2: 'cd-attr',
    sp: '', sp2: '', sp3: '',
  }
  return (
    <>
      {order.map((k) => {
        if (k === 'ind') return tok.ind ? <span key={k}>{'  '.repeat(tok.ind)}</span> : null
        return tok[k] ? <span key={k} className={cls[k]}>{tok[k]}</span> : null
      })}
    </>
  )
}

export function CodeScreen({ s }) {
  const visibleLines = CD_CODE_LINES.slice(0, s.codeLineCount || 0)
  const filesVisible = CD_FILES.filter((f) => (s.fileStage || 0) >= f.step)
  const showPreview = (s.codeLineCount || 0) > 6
  const ringPct = Math.min(100, (s.codeLineCount || 0) / CD_CODE_LINES.length * 62)

  return (
    <div className="scr scr-cd">
      <div className="cd-side">
        <div className="cd-side-hdr">Explorer</div>
        <div className="cd-folder">{Icon.chev} {Icon.folder} calorie-app</div>
        <div style={{ paddingLeft: 16 }}>
          <div className="cd-folder">{Icon.chev} {Icon.folder} src</div>
          {filesVisible.filter((f) => f.id !== 'pkg').map((f, i) => (
            <div key={f.id}
                 className={`cd-file ${f.id === 'page' ? 'active' : ''} ${i === filesVisible.length - 1 && s.fileStage ? 'new' : ''}`}
                 data-bibi-target={`cd-file-${f.id}`}>
              {Icon.file} {f.name}
            </div>
          ))}
          {filesVisible.find((f) => f.id === 'pkg') && (
            <div className="cd-file">{Icon.file} package.json</div>
          )}
        </div>
      </div>

      <div className="cd-editor">
        <div className="cd-tabs">
          <div className="cd-tab active">{Icon.file} CaloriePage.tsx</div>
        </div>
        <div className="cd-code">
          {visibleLines.map((tok, i) => (
            <div key={i} className="cd-code-line">
              <span className="cd-ln-no">{i + 1}</span>
              <div><CodeLine tok={tok} /></div>
            </div>
          ))}
          {(s.codeLineCount || 0) < CD_CODE_LINES.length && (s.codeLineCount || 0) > 0 && (
            <div className="cd-code-line">
              <span className="cd-ln-no">{(s.codeLineCount || 0) + 1}</span>
              <span style={{ width: 4, background: '#ff6a1a', height: 14, display: 'inline-block', animation: 'blink .8s steps(2) infinite' }} />
            </div>
          )}
        </div>
      </div>

      <div className="cd-preview">
        <div className="cd-preview-hdr">
          <span className="dot" /> Preview · localhost:5173
        </div>
        <div className="cd-preview-body">
          {!showPreview ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div className="cd-skeleton" style={{ width: '60%' }} />
              <div className="cd-skeleton" style={{ height: 80 }} />
              <div className="cd-skeleton" style={{ width: '90%' }} />
            </div>
          ) : (
            <div className="cd-app" data-bibi-target="cd-preview-app">
              <div className="cd-app-title">Calories today</div>
              <div className="cd-app-ring" style={{ '--p': `${ringPct}%` }}>
                <div className="num">
                  1,240
                  <small>of 2,000</small>
                </div>
              </div>
              {(s.codeLineCount || 0) > 9 && (
                <>
                  <div className="cd-app-row">
                    <span className="lbl">Breakfast — oats + berries</span>
                    <span className="val">320</span>
                  </div>
                  <div className="cd-app-row">
                    <span className="lbl">Lunch — salmon poke</span>
                    <span className="val">540</span>
                  </div>
                  <div className="cd-app-row">
                    <span className="lbl">Snack — almonds</span>
                    <span className="val">180</span>
                  </div>
                  <div className="cd-app-row">
                    <span className="lbl">Coffee — flat white</span>
                    <span className="val">200</span>
                  </div>
                </>
              )}
              {(s.codeLineCount || 0) > 10 && (
                <div className="cd-app-add">+ Add meal</div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
