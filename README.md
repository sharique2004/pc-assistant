# PC Assistant — A Fully Local Voice AI for Windows

A voice-driven AI assistant for Windows 11 that runs **100% locally on your own
machine**. No external APIs, no cloud calls at runtime, no telemetry. Speak to
it, type to it, and it opens apps, creates files, generates new mini-apps from
a description, searches your computer, and answers questions about system state
— all powered by local Whisper transcription and local Ollama LLM inference.

The codename used internally for the wake-word build is **"Bibi"**.

---

## Highlights

- **Fully offline AI** — Whisper for STT and Ollama (Mistral / Qwen-Coder) for
  intent parsing and code generation, all running on `localhost`.
- **Three-agent architecture** — the project was deliberately split across
  Claude (Flask + React layer), OpenAI's ChatGPT (voice + intent parsing), and
  Firebase Studio / Gemini (Windows execution layer). All three agents handed
  off through a single shared `API_CONTRACT.md`.
- **Windows-native execution** — opens apps, creates files, controls windows,
  reports CPU / memory / disk / active window in real time.
- **Voice replies** — local TTS plays the assistant's response back through the
  default Windows voice engine.
- **Safety rails** — executor refuses paths outside `ALLOWED_PATHS`, never
  deletes anything, and asks for confirmation before destructive or
  long-running actions (e.g. `create_app`).
- **Continuous conversation mode** — optional hands-free re-arm of the
  microphone after each response.

---

## Architecture at a glance

```
+---------------------+        +---------------------------+
|   React frontend    |  HTTP  |     Flask backend         |
|   (Vite, port 5173) +------->+   (Python, port 5000)     |
|                     |        |                           |
|  CommandBar         |        |   /command   /confirm     |
|  StatusFeed         |        |   /system-state  /tts     |
|  SystemPanel        |        |   /health                 |
|  VoicePanel         |        +-------------+-------------+
+---------------------+                      |
                                             | imports
              +------------------------------+------------------------------+
              |               |               |              |              |
              v               v               v              v              v
        voice_intent.py   executor.py    pc_state.py    cloud_router.py  world_model.py
         (local Whisper +  (Windows app   (psutil +     (optional cloud  (SQLite index
          Ollama Mistral)   launching,    pygetwindow)  classifier,      of apps + files)
                            file creation,                disabled by
                            create_app via                default)
                            Ollama Qwen)
```

Optional sidecars: `tts.py` (local Windows SAPI voice), `world_model.py`
(SQLite index of installed apps and recent files for faster resolution),
`desktop_agent.py` (window + system actions).

---

## Prerequisites

| Tool | Purpose | Install |
|---|---|---|
| **Windows 11** | Host OS the executor targets. | — |
| **Python 3.11+** | Backend. | https://python.org/downloads |
| **Node.js 18+** | Frontend. | https://nodejs.org |
| **Ollama** | Local LLM inference. | https://ollama.com/download |
| **FFmpeg** | Required by Whisper. | `winget install --id Gyan.FFmpeg` |
| **Git** | Cloning + pushing. | https://git-scm.com |

After installing Ollama, pull the two models the assistant uses:

```powershell
ollama pull mistral
ollama pull qwen2.5-coder:14b
```

> If your GPU has less than 16 GB of VRAM you can swap `qwen2.5-coder:14b` for a
> smaller variant (e.g. `qwen2.5-coder:7b`) and update `CODEGEN_MODEL` in
> `backend/.env`.

---

## First-time setup

Clone the repo:

```powershell
git clone https://github.com/sharique2004/pc-assistant.git
cd pc-assistant
```

### 1. Backend

```powershell
cd backend

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

copy .env.example .env
# Open .env in an editor and replace every "YOUR_USER" with your Windows
# username so the executor's ALLOWED_PATHS, WORKSPACE_DIR, etc. are correct.
```

### 2. Frontend

```powershell
cd ..\frontend

npm install
copy .env.example .env
```

---

## Running the assistant

Open **two terminals**.

### Terminal 1 — backend

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python app.py
```

The Flask server boots on `http://127.0.0.1:5000`. It will fail gracefully if
Ollama is not yet running — `/health` still returns 200 and the React UI shows
a clear status.

### Terminal 2 — frontend

```powershell
cd frontend
npm run dev
```

The Vite dev server boots on `http://localhost:5173` and proxies all backend
routes through itself, so the browser only ever talks to one origin.

### Terminal 3 (optional) — Ollama

If Ollama is not already running as a service, start it once:

```powershell
ollama serve
```

Open `http://localhost:5173` and tap **Hold to Speak** (or just type a command
into the bar at the bottom of the page).

---

## Things you can say

| Intent | Examples |
|---|---|
| `open_app` | "Open Chrome." "Launch VS Code." "Start Spotify." "Open WhatsApp." (Microsoft Store apps work too — the world model enumerates Start Menu AUMIDs via `Get-StartApps`.) |
| `create_file` | "Create a Python file called `hello.py`." "Make a markdown file named `notes`." |
| `create_app` | "Build me a to-do list app." (requires confirmation before writing) |
| `search_pc` | "Find the resume I saved last week." "Search for `presentation.pptx`." |
| `system_query` | "What apps are running?" "How much memory am I using?" "What is the active window?" |
| **App + action** | "Open ChatGPT and ask how to make pasta." "Open WhatsApp and tell Alex I'm running late." "Open Claude and search for the best speaker." |
| **Coding hand-off** | "Use Claude to build me a React budget tracker." "Use Codex to write a Python script that renames files." Plain "Build me a React dashboard" auto-routes to Claude; plain "Build me a Python CLI tool" auto-routes to Codex. |
| `general` | Anything else — answered by the local Mistral planner that can call any tool above. |

The intent classifier returns a confidence score; anything below
`CLARIFY_THRESHOLD` is routed to a clarification prompt so the assistant asks
the user for more detail instead of guessing.

### Coding hand-off routing

The `create_app` flow chooses between three back-ends based on the request:

| Signal in description | Routed to | Why |
|---|---|---|
| Explicit "use claude" / "claude code" / mentions React, Vue, Next.js, frontend, backend, dashboard, full-stack, mobile app, etc. | **Claude Code CLI** | Better at multi-file projects, UI work, and architectural reasoning. |
| Explicit "use codex" or long script-style request mentioning app / tool / program | **Codex CLI** | Fast for focused scripts and one-shot CLI utilities. |
| Anything else | **Local Ollama** (`qwen2.5-coder:14b`) | Fully offline fallback. |

Both CLI paths return `requires_confirmation: true` so the React UI asks for
your approval before any code is generated.

### Claude Code execution mode

The `CLAUDE_TASK_MODE` env var in `backend/.env` picks how Claude runs:

- **`interactive` (default)** — confirms, then spawns Claude Code in a NEW
  Windows Terminal window with your prompt pre-loaded. You watch it work
  and can interrupt or steer the session. VS Code opens alongside so files
  appear live as Claude writes them. Flask returns immediately; there's no
  captured summary message.
- **`headless`** — runs `claude --print` in the background, captures stdout
  into the `/confirm` response, then opens VS Code on success. Use this when
  you want a batch run (CI, automated tests).

---

## REST API

See [API_CONTRACT.md](./API_CONTRACT.md) for the full schemas. Short version:

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET`  | `/health`       | — | `{ status, service, ollama, cloud_router, world_model }` |
| `POST` | `/command`      | `{ trigger, text? }` *or* multipart `audio` | `{ intent, result }` |
| `POST` | `/confirm`      | `{ operation_id }` | `{ result }` |
| `GET`  | `/system-state` | — | `{ active_window, running_apps, recent_files, cpu_percent, memory, disk, … }` |
| `POST` | `/tts`          | `{ text }` | WAV stream |

---

## Tests

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
pytest
```

The test suite mocks `subprocess`, the Ollama HTTP client, and the audio stack,
so it runs without launching real apps and without needing Ollama or a
microphone.

---

## Project layout

```
pc-assistant/
├── backend/
│   ├── app.py                  # Flask entry point + INTENT_ROUTER
│   ├── voice_intent.py         # Whisper + Ollama intent parsing
│   ├── executor.py             # Windows execution layer
│   ├── pc_state.py             # Live system state
│   ├── world_model.py          # SQLite app + file index
│   ├── cloud_router.py         # Optional cloud decision routing
│   ├── desktop_agent.py        # Window control helpers
│   ├── tts.py                  # Local Windows TTS
│   ├── window_actions.py       # Window-targeted actions
│   ├── requirements.txt
│   ├── .env.example            # Tracked - copy to .env
│   └── test_*.py
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── apiBase.js
│   │   └── components/
│   │       ├── CommandBar.jsx
│   │       ├── StatusFeed.jsx
│   │       ├── SystemPanel.jsx
│   │       ├── VoicePanel.jsx
│   │       └── ConfirmModal.jsx
│   ├── vite.config.js
│   ├── package.json
│   └── .env.example            # Tracked - copy to .env
├── API_CONTRACT.md
├── README.md
├── .gitignore
├── start_bibi_assistant.bat    # Convenience launcher (Windows)
└── install_bibi_startup.ps1    # Optional: add to Windows startup
```

---

## Safety model

The executor follows three non-negotiable rules:

1. **Never delete** any file or folder under any circumstance.
2. **Never write outside `ALLOWED_PATHS`** in `backend/.env`. Anything outside
   is refused.
3. **Anything destructive or long-running asks first.** `create_app`, for
   example, returns `requires_confirmation: true` and waits for a call to
   `POST /confirm` with the operation ID before writing the generated code.

Every executor action also appends to `backend/activity.log` with a UTC
timestamp.

---

## Privacy

- No network calls leave your machine at runtime — STT, intent parsing, and
  code generation all hit `localhost`.
- The optional `cloud_router` is **disabled by default** (`DECISION_ROUTER_MODE=local`)
  and never receives a payload unless you explicitly opt in via `.env`.
- Audio is captured to `pc-assistant/tmp/` and deleted after transcription.

---

## License

Personal project by [@sharique2004](https://github.com/sharique2004). No
license declared yet — please open an issue if you want to reuse the code.
