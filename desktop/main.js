// Bibi — native Windows app shell (Electron).
// Own Chromium engine => reliable keyboard/mouse input, granted microphone,
// and audio autoplay. Spawns the Flask backend and loads the UI. No browser
// chrome, no address bar, no visible localhost.
const { app, BrowserWindow, Menu, session, shell } = require('electron')
const { spawn } = require('child_process')
const net = require('net')
const http = require('http')
const path = require('path')
const fs = require('fs')

// Always load the UI fresh — Electron's HTTP cache otherwise serves a stale
// bundle even when the server sends no-store.
app.commandLine.appendSwitch('disable-http-cache')

const HOST = '127.0.0.1'
const PORT = 5000
const BASE = `http://localhost:${PORT}/`
// Dev: backend sits next to this folder. Packaged/installed: allow an explicit
// override (BIBI_BACKEND_DIR), else look relative to the executable.
let BACKEND_DIR = path.join(__dirname, '..', 'backend')
if (!fs.existsSync(path.join(BACKEND_DIR, 'app.py'))) {
  const candidates = [
    process.env.BIBI_BACKEND_DIR,
    path.join(path.dirname(process.execPath), '..', 'backend'),
    path.join(path.dirname(process.execPath), '..', '..', 'backend'),
  ].filter(Boolean)
  BACKEND_DIR = candidates.find((c) => fs.existsSync(path.join(c, 'app.py'))) || BACKEND_DIR
}

let backendProc = null
let mainWindow = null
let weStartedBackend = false

function portOpen() {
  return new Promise((resolve) => {
    const sock = net.connect(PORT, HOST)
    let done = false
    const finish = (v) => { if (!done) { done = true; try { sock.destroy() } catch (e) {} ; resolve(v) } }
    sock.on('connect', () => finish(true))
    sock.on('error', () => finish(false))
    setTimeout(() => finish(false), 600)
  })
}

async function ensureBackend() {
  if (await portOpen()) return true
  weStartedBackend = true
  backendProc = spawn('pythonw', ['app.py'], {
    cwd: BACKEND_DIR,
    env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    windowsHide: true,
  })
  backendProc.on('error', () => {})
  for (let i = 0; i < 80; i++) {
    if (await portOpen()) return true
    await new Promise((r) => setTimeout(r, 500))
  }
  return await portOpen()
}

function startWake() {
  try {
    const req = http.request(BASE + 'wake/start', { method: 'POST' }, () => {})
    req.on('error', () => {})
    req.end()
  } catch (e) {}
}

function stopBackend() {
  if (weStartedBackend && backendProc) {
    try { backendProc.kill() } catch (e) {}
  }
}

function createWindow() {
  // Grant microphone (and other) permissions automatically — this is a local,
  // trusted, single-purpose app, so getUserMedia/mic just works (no prompt).
  session.defaultSession.setPermissionRequestHandler((wc, permission, cb) => cb(true))
  session.defaultSession.setPermissionCheckHandler(() => true)

  mainWindow = new BrowserWindow({
    width: 1200,
    height: 820,
    minWidth: 980,
    minHeight: 660,
    backgroundColor: '#0D0D1C',
    title: 'Bibi',
    icon: path.join(__dirname, 'build', 'icon.ico'),
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      autoplayPolicy: 'no-user-gesture-required', // Bibi can speak without a click
      contextIsolation: true,
      nodeIntegration: false,
    },
  })
  Menu.setApplicationMenu(null)
  mainWindow.once('ready-to-show', () => mainWindow.show())
  // Keep the window/taskbar title as "Bibi" (don't follow the page <title>).
  mainWindow.on('page-title-updated', (e) => e.preventDefault())
  mainWindow.loadURL(BASE)
  // Links that try to open a new window go to the user's real browser.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })
  mainWindow.on('closed', () => { mainWindow = null })
}

// Single instance — second launch focuses the existing window.
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWindow) { if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.focus() }
  })

  app.whenReady().then(async () => {
    const ok = await ensureBackend()
    if (ok) startWake()
    createWindow()
  })

  app.on('window-all-closed', () => {
    stopBackend()
    app.quit()
  })
  app.on('before-quit', stopBackend)
}
