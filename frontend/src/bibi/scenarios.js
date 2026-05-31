// Scripted demo flows — each scenario walks the cursor through a real-looking
// browser screen so we can visualize what a future agentic Bibi would do.
// The mic button in the rail still drives the real backend.

export const SCENARIOS = {
  whatsapp: {
    id: 'whatsapp',
    title: 'Message Sam on WhatsApp',
    chip: 'Message Sam on WhatsApp',
    glyph: '💬',
    url: 'web.whatsapp.com/messages/sam',
    tab: 'WhatsApp Web',
    screen: 'whatsapp',
    initial: { searchQuery: '', selectedChat: null, draft: '', sentMessage: '',
               searchFocused: false, composeFocused: false },
    completion: 'Message sent to Sam',
    steps: [
      { label: 'Navigate to web.whatsapp.com', action: 'wait', duration: 700 },
      { label: 'Focus the chat search', action: 'click', target: 'wa-search',
        doneState: { searchFocused: true } },
      { label: 'Type "Sam" to find the contact', action: 'type', target: 'wa-search',
        field: 'searchQuery', text: 'Sam', cps: 9 },
      { label: 'Open Sam\'s conversation', action: 'click', target: 'wa-chat-sam',
        doneState: { selectedChat: 'sam', searchFocused: false, searchQuery: '' } },
      { label: 'Focus the message composer', action: 'click', target: 'wa-compose',
        doneState: { composeFocused: true } },
      { label: 'Compose a friendly message', action: 'type', target: 'wa-compose',
        field: 'draft', text: 'hey! free to grab coffee tomorrow ☕', cps: 14 },
      { label: 'Hit send', action: 'click', target: 'wa-send',
        doneState: (s) => ({ sentMessage: s.draft, draft: '', composeFocused: false }) },
      { label: 'Delivered ✓✓', action: 'wait', duration: 600 },
    ],
  },

  flights: {
    id: 'flights',
    title: 'Book a flight to Dubai',
    chip: 'Book me a flight to Dubai',
    glyph: '✈',
    url: 'google.com/travel/flights',
    tab: 'Google Flights',
    screen: 'flights',
    initial: { destQuery: '', destFocused: false, suggestHighlight: false,
               searched: false, selectedFlight: null, bookingConfirmed: false },
    completion: 'Holding seat on EK225 — review and confirm',
    steps: [
      { label: 'Open Google Flights', action: 'wait', duration: 700 },
      { label: 'Focus the destination field', action: 'click', target: 'fl-dest',
        doneState: { destFocused: true } },
      { label: 'Type "Dubai"', action: 'type', target: 'fl-dest',
        field: 'destQuery', text: 'Dubai', cps: 8 },
      { label: 'Select Dubai (DXB)', action: 'click', target: 'fl-suggest-dxb',
        preState: { suggestHighlight: true },
        doneState: { destFocused: false, destQuery: 'Dubai (DXB)', suggestHighlight: false } },
      { label: 'Search for flights', action: 'click', target: 'fl-search',
        doneState: { searched: true }, postWait: 800 },
      { label: 'Compare best departures', action: 'move', target: 'fl-result-1', duration: 700 },
      { label: 'Pick the nonstop Emirates flight', action: 'click', target: 'fl-result-0',
        doneState: { selectedFlight: 0, bookingConfirmed: true } },
      { label: 'Holding seat 14A · review to confirm', action: 'wait', duration: 700 },
    ],
  },

  chatgpt: {
    id: 'chatgpt',
    title: 'Open ChatGPT',
    chip: 'Open ChatGPT',
    glyph: '✦',
    url: 'chatgpt.com',
    tab: 'ChatGPT',
    screen: 'chatgpt',
    initial: { prompt: '', promptFocused: false },
    completion: 'ChatGPT is open and ready',
    steps: [
      { label: 'Open chatgpt.com', action: 'wait', duration: 800 },
      { label: 'Page loaded — focus prompt', action: 'click', target: 'gpt-input',
        doneState: { promptFocused: true } },
      { label: 'Ready when you are', action: 'wait', duration: 600 },
    ],
  },

  calorie: {
    id: 'calorie',
    title: 'Build a calorie tracker',
    chip: 'Create an app that tracks calories',
    glyph: '⌘',
    url: 'localhost:5173/~/calorie-app',
    tab: 'calorie-app — Cursor',
    screen: 'code',
    initial: { codeLineCount: 0, fileStage: 0 },
    completion: 'calorie-app scaffolded · preview live',
    steps: [
      { label: 'Spin up a new Vite + React + TS project', action: 'wait', duration: 800,
        doneState: { fileStage: 0, codeLineCount: 0 } },
      { label: 'Create CaloriePage.tsx', action: 'wait', duration: 500,
        doneState: { fileStage: 1 } },
      { label: 'Scaffold the page component', action: 'codeReveal', upTo: 6, duration: 1200 },
      { label: 'Wire state for eaten/goal/percent', action: 'codeReveal', upTo: 10, duration: 1000 },
      { label: 'Add MealList + meals.ts', action: 'wait', duration: 500,
        doneState: { fileStage: 3 } },
      { label: 'Render the dashboard', action: 'codeReveal', upTo: 14, duration: 1100 },
      { label: 'Preview is live · localhost:5173', action: 'wait', duration: 700 },
    ],
  },
}

// Map a free-form transcript (from the real backend) to a scenario id. This
// lets the existing voice pipeline trigger the scripted flow without changing
// the executor — the demo still proves the end-to-end UX.
export function matchScenario(text) {
  if (!text) return null
  const t = text.toLowerCase()
  if (/whats\s*app|message\s+\w+\s+on/.test(t)) return 'whatsapp'
  if (/flight|book\s+(me\s+)?a?\s*flight|dubai/.test(t)) return 'flights'
  if (/chat\s*gpt|open\s+chat/.test(t)) return 'chatgpt'
  if (/calorie|tracks?\s+calories|track\s+my\s+food/.test(t)) return 'calorie'
  return null
}
