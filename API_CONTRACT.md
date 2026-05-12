# PC Assistant — API Contract

**Version:** 1.2  
**Base URL:** `http://localhost:5000` (set via `FLASK_HOST` / `FLASK_PORT` in `backend/.env`)  
**Content-Type:** JSON for text commands and most endpoints; `multipart/form-data` for browser-uploaded audio commands.  
**Auth:** None — this API is local-only and not exposed to the internet.

This document is the single source of truth shared by all three agents
(Claude, OpenAI, Firebase Studio).  No module may break the contracts defined
here without updating this file and informing the other agents.

---

## Table of Contents

1. [GET /health](#get-health)
2. [POST /command](#post-command)
3. [GET /system-state](#get-system-state)
4. [POST /confirm](#post-confirm)
5. [Intent Object Shape](#intent-object-shape)
6. [Executor Result Shape](#executor-result-shape)
7. [Error Envelope](#error-envelope)
8. [Intent Categories & Parameter Shapes](#intent-categories--parameter-shapes)

---

## GET /health

Liveness probe.  Reports both Flask status and whether Ollama is reachable.
Used by the React app on startup to decide whether to enable the command bar.

### Request
No body, no query parameters.

### Response — 200 OK

The HTTP status is **always 200**.  Check `ollama.reachable` to know whether
local inference is available.

```json
{
  "status":  "ok",
  "service": "pc-assistant-backend",
  "ollama": {
    "reachable": true,
    "host":      "http://localhost:11434",
    "error":     null
  }
}
```

| Field              | Type         | Description                                         |
|--------------------|--------------|-----------------------------------------------------|
| `status`           | `"ok"`       | Always `"ok"` — Flask is running.                  |
| `service`          | string       | Service identifier.                                 |
| `ollama.reachable` | boolean      | `true` if Ollama responded within the cached health timeout. |
| `ollama.host`      | string       | Value of `OLLAMA_HOST` env var.                     |
| `ollama.error`     | string\|null | Human-readable error if unreachable, else `null`.   |

---

## POST /command

Main command pipeline.  Accepts typed text, browser-uploaded audio, or backend
microphone capture and returns the same intent/result envelope for each path.

**Flow:**
1. Frontend sends typed text, uploaded audio, or a trigger for backend mic capture.
2. Backend calls `voice_intent.parse_text_command`, `parse_audio_file`, or `listen_and_parse`.
3. Backend routes the returned intent through `INTENT_ROUTER` in `app.py`.
4. The executor function result is returned alongside the intent.

### Request Body - typed command

```json
{
  "trigger": "typed_text",
  "text": "use codex to build a React budget tracker"
}
```

| Field     | Type   | Required | Description                                        |
|-----------|--------|----------|----------------------------------------------------|
| `text`    | string | **yes**  | Natural-language command to parse and execute.     |
| `trigger` | string | no       | Defaults to `"typed_text"` when `text` is present. |

Aliases accepted for `text`: `command`, `transcript`.

### Request Body - uploaded audio

Send `multipart/form-data` with:

| Field     | Type   | Required | Description                                |
|-----------|--------|----------|--------------------------------------------|
| `audio`   | file   | **yes**  | WAV/WebM/MP3/M4A/etc. recorded by browser. |
| `trigger` | string | no       | Defaults to `"tap_to_speak"`.              |

### Request Body - backend microphone capture

```json
{
  "trigger": "hold_to_speak"
}
```

| Field     | Type   | Required | Description                                              |
|-----------|--------|----------|----------------------------------------------------------|
| `trigger` | string | **yes**  | How capture was initiated. Currently: `"hold_to_speak"`. |

### Response — 200 OK (success)

```json
{
  "success": true,
  "intent": {
    "intent":         "open_app",
    "parameters":     { "app_name": "Notepad" },
    "raw_transcript": "open notepad please",
    "trigger":        "hold_to_speak",
    "confidence":     0.95
  },
  "result": {
    "success": true,
    "message": "Opened Notepad.",
    "data":    { "app_name": "Notepad", "exe_path": "C:/Windows/System32/notepad.exe" }
  }
}
```

### Response — 200 OK (requires confirmation)

Returned when the executor determines an action is irreversible and needs
explicit user approval.  The frontend must show a confirmation dialog and call
`POST /confirm` if the user approves.

```json
{
  "success": true,
  "intent": { "intent": "create_file", "parameters": { ... }, "..." : "..." },
  "result": {
    "success": false,
    "message": "This action requires your confirmation.",
    "data": {
      "requires_confirmation": true,
      "operation_id":          "a3f1c2d4-7e8b-4c1a-b2f3-9d0e1f2a3b4c",
      "description":           "Create notes.txt in C:/Users/Me/Documents"
    }
  }
}
```

### Response — 200 OK (clarification needed)

Returned when `voice_intent` sets `intent = "clarify"` due to low confidence.

```json
{
  "success": true,
  "intent": {
    "intent":         "clarify",
    "parameters":     { "follow_up": "Did you say 'open' or 'close'?" },
    "raw_transcript": "open... or... um",
    "confidence":     0.28
  },
  "result": {
    "success": true,
    "message": "Clarification needed.",
    "data":    { "requires_clarification": true, "follow_up": "Did you say 'open' or 'close'?" }
  }
}
```

### Response — 400 Bad Request

```json
{
  "success": false,
  "error":   "missing_field",
  "detail":  "'trigger' is a required non-empty string."
}
```

| `error` code    | Cause                              |
|-----------------|------------------------------------|
| `invalid_json`  | Body is not valid JSON.            |
| `missing_field` | Required field absent or empty.    |

### Response — 500 Internal Server Error

```json
{
  "success": false,
  "error":   "intent_parse_failed",
  "detail":  "<exception message>"
}
```

| `error` code          | Cause                                            |
|-----------------------|--------------------------------------------------|
| `intent_parse_failed` | `voice_intent.listen_and_parse()` raised.        |
| `executor_failed`     | The executor handler raised an exception.        |

---

## GET /system-state

Returns a live snapshot of the host machine.  Polled by `SystemPanel` every
`VITE_POLL_INTERVAL_MS` ms (default 5 000 ms).

### Request
No body, no query parameters.

### Response — 200 OK

```json
{
  "active_window": "Visual Studio Code",
  "running_apps":  ["chrome.exe", "code.exe", "spotify.exe"],
  "recent_files":  ["resume_v2.pdf", "notes.txt"],
  "cpu_percent":   34.2,
  "memory": {
    "total_gb": 16.0,
    "used_gb":   7.3,
    "percent":  45.6
  },
  "disk": {
    "total_gb": 512.0,
    "used_gb":  210.5,
    "percent":   41.1
  },
  "timestamp": "2026-04-08T10:30:00.000000"
}
```

| Field           | Type          | Description                                         |
|-----------------|---------------|-----------------------------------------------------|
| `active_window` | string        | Title of the foreground window.                     |
| `running_apps`  | array[string] | Sorted, deduplicated `.exe` process names.          |
| `recent_files`  | array[string] | File names (not paths), newest-first.               |
| `cpu_percent`   | float         | CPU utilisation 0–100.                              |
| `memory`        | object        | `total_gb`, `used_gb`, `percent`.                   |
| `disk`          | object        | `total_gb`, `used_gb`, `percent`.                   |
| `timestamp`     | string        | ISO-8601 UTC timestamp of the snapshot.             |

### Response — 500 Internal Server Error

```json
{
  "success": false,
  "error":   "state_fetch_failed",
  "detail":  "<exception message>"
}
```

---

## POST /confirm

Second step for operations that returned `requires_confirmation: true`.
The frontend calls this only after the user explicitly approves.

### Request Body

```json
{
  "operation_id": "a3f1c2d4-7e8b-4c1a-b2f3-9d0e1f2a3b4c"
}
```

| Field          | Type   | Required | Description                              |
|----------------|--------|----------|------------------------------------------|
| `operation_id` | string | **yes**  | UUID from the prior `/command` response. |

### Response — 200 OK

```json
{
  "success": true,
  "result": {
    "success": true,
    "message": "Created notes.txt in C:/Users/Me/Documents.",
    "data":    { "file_path": "C:/Users/Me/Documents/notes.txt" }
  }
}
```

### Response — 400 Bad Request

```json
{ "success": false, "error": "missing_field", "detail": "'operation_id' is required." }
```

### Response — 500 Internal Server Error

```json
{ "success": false, "error": "confirm_failed", "detail": "<exception message>" }
```

---

## Intent Object Shape

Produced by `voice_intent.listen_and_parse()`.  Consumed by `app.py` routing
and by the React `StatusFeed` component for display.

```json
{
  "intent":         "<intent_name>",
  "parameters":     { "<key>": "<value>" },
  "raw_transcript": "<verbatim whisper output>",
  "trigger":        "<trigger value>",
  "confidence":     0.95
}
```

| Field            | Type   | Description                                                           |
|------------------|--------|-----------------------------------------------------------------------|
| `intent`         | string | One of the known intent names (see table below).                      |
| `parameters`     | object | Intent-specific key/value pairs.                                      |
| `raw_transcript` | string | Verbatim text from Whisper before LLM processing.                     |
| `trigger`        | string | Echo of the trigger argument passed to `listen_and_parse`.            |
| `confidence`     | float  | 0.0–1.0. Values below `CLARIFY_THRESHOLD` cause intent → `"clarify"`. |

---

## Executor Result Shape

Every function in `executor.py` must return a dict matching this shape.
No exceptions may propagate — catch everything and return an error dict.

```json
{
  "success": true,
  "message": "<human-readable outcome shown in the UI>",
  "data":    {}
}
```

Optional fields for deferred operations:

```json
{
  "success": false,
  "message": "This action requires your confirmation.",
  "data": {
    "requires_confirmation": true,
    "operation_id":          "<uuid>",
    "description":           "<what will happen if confirmed>"
  }
}
```

---

## Error Envelope

All error responses from the backend share this shape.

```json
{
  "success": false,
  "error":   "<snake_case_error_code>",
  "detail":  "<Human-readable description.>"
}
```

Frontend code should treat a response as failed when `!response.ok || data.success === false`.

---

## Intent Categories & Parameter Shapes

| Intent         | Executor function        | Parameter shape                                              |
|----------------|--------------------------|--------------------------------------------------------------|
| `open_app`     | `executor.open_app`      | `{ "app_name": string }`                                    |
| `create_file`  | `executor.create_file`   | `{ "file_name": string, "file_type": string, "content": string }` |
| `create_app`   | `executor.create_app`    | `{ "description": string }`                                 |
| `search_pc`    | `executor.search_pc`     | `{ "query": string }`                                       |
| `web_search`   | `executor.web_search`    | `{ "query": string }`                                       |
| `system_query` | `executor.system_query`  | `{ "query": string }`                                       |
| `general`      | `executor.general`       | `{ "raw_text": string }`                                    |
| `clarify`      | *(handled in app.py)*    | `{ "follow_up": string }`                                   |

`general` may use local planner tools for multi-step work, including a
confirmed `codex_task` handoff that runs the local Codex CLI in a generated
workspace folder.

### Safety rules enforced by executor.py

1. **No deletes** — the executor must never delete any file.
2. **Path allow-list** — all writes must be within `ALLOWED_PATHS` (`.env`).
3. **Confirmation required** — any irreversible write returns
   `requires_confirmation: true` and queues via `_queue_operation()`.
   The user must call `POST /confirm` before the action executes.
