const configuredApiBase = (import.meta.env.VITE_API_BASE_URL || '').trim().replace(/\/$/, '')

// In development we intentionally use relative URLs so Vite proxies requests
// to Flask and the browser never hits a cross-origin backend directly.
const apiBase = import.meta.env.DEV ? '' : configuredApiBase

export default apiBase
