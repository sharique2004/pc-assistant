"""
orchestrator.py — Bibi's single brain-to-action loop.

One utterance (from wake word, mic, or typed text) flows through here:

    transcript ─▶ agent_brain.plan()
                    ├─ question ─▶ answer aloud (TTS in the UI)
                    └─ tasks ────▶ run each task IN ORDER, showing a live
                                   workflow (this step → done → next)

Web tasks run in the real Playwright browser (browser_agent.cockpit) so the
side panel shows an actual browser doing the work.  Desktop tasks (open app,
send message) go through the existing executor.  Nothing is ever closed.

The whole UI polls ONE endpoint — /agent/status — which returns the current
workflow, the wake-listening state, and a queue of lines for the UI to speak.

Python 3.11+
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import agent_brain
import browser_agent
import executor
import journal

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._wf: dict[str, Any] | None = None
        self._wf_counter = 0
        self._speak: list[dict] = []
        self._speak_counter = 0
        self._listening = False
        self._wake_status = "Wake listening is off."
        self._wake_ping = 0       # bumps when the wake word is heard (chime: listening)
        self._process_ping = 0    # bumps when capture stops & work begins (chime: working)
        self._busy = threading.Lock()

    # ---- state the UI polls ----------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "seq": self._seq,
                "listening": self._listening,
                "wake_status": self._wake_status,
                "wake_ping": self._wake_ping,
                "process_ping": self._process_ping,
                "workflow": dict(self._wf) if self._wf else None,
                "speak": list(self._speak[-8:]),
            }

    def set_wake_state(self, listening: bool, status_text: str | None = None) -> None:
        with self._lock:
            self._listening = bool(listening)
            if status_text is not None:
                self._wake_status = status_text
            self._bump()

    def signal_wake(self, status_text: str = "Wake word heard.") -> None:
        """Wake word just fired — bump a ping the UI watches to play a chime."""
        with self._lock:
            self._wake_ping += 1
            self._listening = True
            self._wake_status = status_text
            self._bump()

    def signal_processing(self, status_text: str = "Got it — working on it…") -> None:
        """Capture finished, work is starting — UI plays the 'working' sound."""
        with self._lock:
            self._process_ping += 1
            self._wake_status = status_text
            self._bump()

    def speak(self, text: str) -> None:
        """Public: queue a line for the UI to speak aloud."""
        self._speak_line(text)

    # ---- entry point ------------------------------------------------------
    def run_utterance(self, transcript: str, source: str = "text") -> dict:
        text = " ".join(str(transcript or "").split()).strip()
        if not text:
            return self.status()
        threading.Thread(
            target=self._run, args=(text, source), daemon=True, name="agent-run"
        ).start()
        return self.status()

    # ---- internals --------------------------------------------------------
    def _bump(self) -> None:
        self._seq += 1

    def _speak_line(self, text: str) -> None:
        line = " ".join(str(text or "").split()).strip()
        if not line:
            return
        with self._lock:
            self._speak_counter += 1
            self._speak.append({"id": self._speak_counter, "text": line})
            if len(self._speak) > 30:
                self._speak = self._speak[-30:]
            self._bump()

    def _new_workflow(self, transcript: str, source: str) -> None:
        with self._lock:
            self._wf_counter += 1
            self._wf = {
                "id": self._wf_counter,
                "source": source,
                "transcript": transcript,
                "kind": "",
                "status": "planning",
                "message": "Thinking…",
                "tasks": [],
                "active": -1,
            }
            self._bump()

    def _set_wf(self, **fields: Any) -> None:
        with self._lock:
            if self._wf is None:
                return
            self._wf.update(fields)
            self._bump()

    def _set_task_status(self, index: int, status: str, detail: str = "") -> None:
        with self._lock:
            if self._wf is None:
                return
            tasks = self._wf.get("tasks", [])
            if 0 <= index < len(tasks):
                tasks[index]["status"] = status
                if detail:
                    tasks[index]["detail"] = detail
            self._wf["active"] = index if status == "active" else self._wf.get("active", -1)
            self._bump()

    def _screen_context(self) -> str:
        """Title of the currently focused window, so the planner can act
        'from where you are' (e.g. compose an email when Gmail is open)."""
        try:
            import window_actions
            title = window_actions.get_active_window_title()
            return title if title and title != "Unknown" else ""
        except Exception:  # noqa: BLE001
            return ""

    _ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
                 "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
                 "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5}

    def _parse_ordinal(self, text: str) -> int:
        import re
        low = str(text or "").lower()
        for word, n in self._ORDINALS.items():
            if re.search(rf"\b{word}\b", low):
                return n
        m = re.search(r"\b(?:number|#)\s*(\d+)\b", low) or re.search(r"\b(\d+)(?:st|nd|rd|th)\b", low)
        return int(m.group(1)) if m else 0

    def _is_generic_play_title(self, title: str) -> bool:
        """True if 'title' is just navigation words (e.g. 'Play first video') and
        not an actual video/result name."""
        import re
        words = re.findall(r"[a-z0-9']+", str(title or "").lower())
        if not words:
            return True
        stop = {"play", "open", "watch", "click", "the", "a", "an", "video", "videos",
                "one", "result", "results", "item", "this", "that", "please", "can",
                "you", "on", "page", "it", "show", "select"} | set(self._ORDINALS)
        return all(w in stop for w in words)

    def _site_search_url(self, site: str, query: str) -> str:
        """Direct on-site search URL for known sites — instant and reliable
        (searches ON the site, never the web)."""
        import urllib.parse
        q = urllib.parse.quote_plus(str(query or "").strip())
        s = str(site or "").strip().lower()
        templates = {
            "amazon": "https://www.amazon.com/s?k={q}",
            "youtube": "https://www.youtube.com/results?search_query={q}",
            "google": "https://www.google.com/search?q={q}",
            "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}",
            "netflix": "https://www.netflix.com/search?q={q}",
            "crunchyroll": "https://www.crunchyroll.com/search?q={q}",
            "prime": "https://www.primevideo.com/search/ref=atv_nb_sr?phrase={q}",
            "primevideo": "https://www.primevideo.com/search/ref=atv_nb_sr?phrase={q}",
            "reddit": "https://www.reddit.com/search/?q={q}",
            "flipkart": "https://www.flipkart.com/search?q={q}",
            "spotify": "https://open.spotify.com/search/{q}",
            "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
            "twitter": "https://twitter.com/search?q={q}",
            "x": "https://x.com/search?q={q}",
            "github": "https://github.com/search?q={q}&type=repositories",
            "maps": "https://www.google.com/maps/search/{q}",
            "google maps": "https://www.google.com/maps/search/{q}",
            "linkedin": "https://www.linkedin.com/search/results/all/?keywords={q}",
        }
        tpl = templates.get(s, "")
        return tpl.format(q=q) if tpl else ""

    def _resolve_site_url(self, site: str) -> str:
        """Turn a spoken site name into a URL (works for any site)."""
        s = str(site or "").strip().lower()
        try:
            from browser_agent import SITE_CONFIG
            if s in SITE_CONFIG:
                return SITE_CONFIG[s]["home"]
        except Exception:
            pass
        known = {
            "amazon": "https://www.amazon.com", "youtube": "https://www.youtube.com",
            "google": "https://www.google.com", "gmail": "https://mail.google.com",
            "reddit": "https://www.reddit.com", "ebay": "https://www.ebay.com",
            "flipkart": "https://www.flipkart.com", "wikipedia": "https://www.wikipedia.org",
            "netflix": "https://www.netflix.com", "spotify": "https://open.spotify.com",
        }
        if s in known:
            return known[s]
        if "." in s:
            return s if s.startswith("http") else "https://" + s
        return f"https://www.{s}.com"

    def _media_site_hint(self) -> str:
        """Infer which media site we're on from the controlled browser's URL,
        falling back to the focused window title."""
        for source in (
            (browser_agent.cockpit.status().get("url") if browser_agent.cockpit.is_running() else ""),
            self._screen_context(),
        ):
            low = str(source or "").lower()
            for key in ("youtube", "netflix", "crunchyroll", "prime"):
                if key in low:
                    return key
        return ""

    def _detect_current_site(self) -> str:
        """Which KNOWN site is in front right now (focused window / browser
        title), so "search X" can use that site's own search instead of guessing
        where the search box is. Returns a key for _site_search_url, else ''."""
        ctx = self._screen_context().lower()
        try:
            if browser_agent.cockpit.is_running():
                ctx += " " + str(browser_agent.cockpit.status().get("url", "")).lower()
        except Exception:  # noqa: BLE001
            pass
        # Most specific first; "google" last so youtube/maps/gmail don't grab it.
        for key in ("crunchyroll", "youtube", "netflix", "primevideo", "prime video",
                    "amazon", "flipkart", "ebay", "spotify", "reddit", "github",
                    "linkedin", "wikipedia", "google maps", "maps", "google"):
            if key in ctx:
                return {"prime video": "primevideo", "google maps": "maps"}.get(key, key)
        return ""

    def _run(self, transcript: str, source: str) -> None:
        # Serialize workflows so two utterances don't interleave on the browser.
        with self._busy:
            self._new_workflow(transcript, source)
            context = self._screen_context()
            if context:
                self._set_wf(message=f"On it… (you're in {context[:48]})")
            try:
                plan = agent_brain.plan(transcript, context=context)
            except Exception as exc:  # noqa: BLE001
                self._set_wf(status="error", message=f"Planning failed: {exc}")
                self._speak_line("Sorry, I couldn't work that out.")
                return

            kind = plan.get("kind", "tasks")

            if kind == "question":
                self._set_wf(kind="question", status="running", message="Looking that up…")
                answer = agent_brain.answer_question(transcript)
                self._set_wf(status="done", message=answer)
                self._speak_line(answer)
                journal.log_interaction(transcript, "question", answer)
                return

            tasks = plan.get("tasks", [])
            if not tasks:
                self._set_wf(status="done", message="I didn't find anything to do.")
                self._speak_line("I didn't catch a task there.")
                journal.log_interaction(transcript, "question", "(no actionable task found)")
                return

            ui_tasks = [{"title": t["title"], "action": t["action"], "status": "pending", "detail": ""} for t in tasks]
            self._set_wf(kind="tasks", status="running", tasks=ui_tasks,
                         message=f"Running {len(tasks)} task" + ("s" if len(tasks) != 1 else "") + "…")

            for i, task in enumerate(tasks):
                self._set_task_status(i, "active")
                if task.get("speak"):
                    self._speak_line(task["speak"])
                try:
                    detail = self._execute_task(task)
                    self._set_task_status(i, "done", detail)
                    self._set_wf(message=detail or task["title"])
                except Exception as exc:  # noqa: BLE001
                    logger.exception("task failed")
                    self._set_task_status(i, "error", str(exc))
                    self._speak_line(f"I hit a problem with {task.get('title','that task')}.")
                time.sleep(0.4)

            self._set_wf(status="done", message="All done.")
            self._speak_line("All done.")
            # Journal the whole interaction (what was heard + each task + result).
            final = dict(self._wf) if self._wf else {}
            journal.log_interaction(transcript, "tasks", final.get("message", "All done."),
                                    final.get("tasks", []))

    def _execute_task(self, task: dict) -> str:
        action = task.get("action", "")

        if action in ("open_site", "open_url"):
            url = task.get("url") or task.get("query") or task.get("task") or ""
            res = executor.open_url(url)            # NEW TAB in Brave
            return str(res.get("message", "")) or f"Opened {url}"

        if action == "web_search":
            query = task.get("query") or task.get("title") or ""
            res = executor.web_search(query)        # Brave (deep-links preserved)
            return str(res.get("message", "")) or f"Searched for {query}"

        # ── Act on whatever is ON YOUR SCREEN (real cursor, any window) ──
        if action == "media_search":
            site = task.get("site", "")
            query = task.get("query", "") or task.get("title", "")
            if not query:
                return "Search for what?"
            # FAST PATH: known sites have a direct search URL — open it straight
            # in Brave. Instant, reliable, and it searches ON the site (not the web).
            direct = self._site_search_url(site, query)
            if direct:
                executor.open_url(direct)
                return f"Searching {site} for “{query}”."
            # Unknown site → open it, let it load, then find its search box by sight.
            import screen_control
            on_site = site and site in self._screen_context().lower()
            if site and not on_site:
                executor.open_url(self._resolve_site_url(site))
                time.sleep(float(__import__("os").getenv("BIBI_SITE_LOAD_S", "4.0")))
            r = screen_control.search_here(query)
            if not r.get("ok"):
                return r.get("message", "I couldn't find a search box on that site.")
            return f"Searching {site or 'this page'} for “{query}”."

        if action == "play_video":
            import screen_control
            transcript = (self._wf or {}).get("transcript", "")
            # Index: explicit field, else parse the ordinal from the command.
            idx_s = str(task.get("index", "")).strip()
            idx = int(idx_s) if idx_s.isdigit() else self._parse_ordinal(transcript + " " + task.get("title", ""))
            # Only treat the title as a real video title if it's not generic
            # navigation words like "Play first video".
            title = task.get("title", "").strip()
            if self._is_generic_play_title(title):
                title = ""
            if title:
                target = f'the result/option titled "{title}"'
            else:
                target = (f"the {screen_control.ordinal(idx or 1)} item in the main list of "
                          "results/options/videos/products visible on this page — count visible "
                          "entries from the top (left-to-right, top-to-bottom) and click its title or thumbnail")
            # Make the right tab visible first ONLY if the page isn't already a
            # content page (the Bibi UI is itself a tab). If we're already on a
            # results page, don't switch away from it.
            screen_control.focus_browser()
            ctx = self._screen_context().lower()
            if any(b in ctx for b in ("bibi", "voice assistant", "new tab")) or not ctx:
                tab_hint = task.get("site", "") or self._media_site_hint() or "YouTube"
                screen_control.activate_tab(tab_hint)
            time.sleep(0.3)
            r = screen_control.click_target(target)
            if not r.get("ok"):
                return r.get("message", "I couldn't find that one on the page.")
            return f"Opening {('“'+title+'”') if title else ('the '+screen_control.ordinal(idx or 1)+' option')}."

        if action == "search_here":
            import screen_control
            query = task.get("query", "") or task.get("title", "")
            if not query:
                return "Search for what?"
            # 1) If a KNOWN site is in front (YouTube, Crunchyroll, Netflix, …),
            #    use ITS direct search URL — instant and reliable, no guessing
            #    where the search box is. This is the fix for "search on this page".
            site = self._detect_current_site()
            if site:
                direct = self._site_search_url(site, query)
                if direct:
                    executor.open_url(direct)
                    return f"Searching {site} for “{query}”."
            # 2) Otherwise, find this page's own search box by sight.
            r = screen_control.search_here(query)
            if not r.get("ok"):
                # No search box on the current page → fall back to a web search.
                executor.web_search(query)
                return f"I didn't see a search box here, so I searched the web for “{query}”."
            return f"Searched this page for “{query}”. Tell me which result to open."

        if action == "click_on_screen":
            import screen_control
            target = task.get("target", "") or task.get("title", "") or task.get("text", "")
            r = screen_control.click_target(target)
            if not r.get("ok"):
                return r.get("message", f"I couldn't find “{target}” on screen.")
            return f"Clicked {r.get('label') or target}."

        if action == "describe_screen":
            import screen_agent
            desc = screen_agent.describe()
            self._speak_line(desc)
            return desc

        if action == "do_on_screen":
            import screen_agent
            goal = task.get("goal", "") or task.get("task", "") or task.get("target", "") or task.get("title", "")

            def _narrate(say, _act):
                self._speak_line(say)
                self._set_wf(message=say)

            r = screen_agent.run_goal(goal, on_step=_narrate)
            return str(r.get("message", "")) or "Done with what I could see."

        if action == "type_on_screen":
            import screen_control
            screen_control.type_text(task.get("text", ""), enter=str(task.get("submit", "")).lower() in ("1", "true", "yes"))
            return "Typed it."

        if action == "scroll_screen":
            import screen_control
            direction = str(task.get("direction", "down")).lower()
            screen_control.scroll(-700 if direction != "up" else 700)
            return f"Scrolled {direction}."

        if action == "open_browser":
            res = executor.open_url("https://www.google.com")
            return str(res.get("message", "")) or "Opened your browser."

        if action == "remember":
            import profile_store
            return profile_store.remember(task.get("key", ""), task.get("value", ""))

        if action == "compose_email":
            res = executor.compose_email(
                to=task.get("to", "") or task.get("contact", ""),
                subject=task.get("subject", ""),
                body=task.get("body", "") or task.get("message", ""),
            )
            return str(res.get("message", "")) or "Drafted your email."

        if action == "open_app":
            res = executor.open_app(task.get("app", "") or task.get("task", ""))
            return str(res.get("message", "")) or "Opened the app."

        if action == "app_search":
            res = executor.automate_app_search(task.get("app", ""), task.get("query", ""))
            return str(res.get("message", "")) or "Searched in the app."

        if action == "send_message":
            res = executor.automate_app_message(
                task.get("app", "WhatsApp"), task.get("contact", ""), task.get("message", "")
            )
            return str(res.get("message", "")) or "Message handled."

        if action == "search_pc":
            res = executor.search_pc(task.get("query", "") or task.get("task", ""))
            return str(res.get("message", "")) or "Searched your PC."

        if action == "system_query":
            res = executor.system_query(task.get("query", "") or task.get("task", ""))
            return str(res.get("message", "")) or "Here's your system state."

        if action == "create_file":
            res = executor.create_file(
                file_name=task.get("file_name", "note"),
                file_type=task.get("file_type", "txt") or "txt",
                content=task.get("content", ""),
            )
            return str(res.get("message", "")) or "Created the file."

        if action == "create_app":
            res = executor.create_app(task.get("task", "") or task.get("title", ""))
            return str(res.get("message", "")) or "Building the app."

        if action == "type_in_window":
            res = executor.automate_window_text(
                task.get("window", "current window") or "current window",
                task.get("text", ""),
                submit=False,
            )
            return str(res.get("message", "")) or "Typed it."

        if action == "click_button":
            res = executor.automate_click_button(
                task.get("window", "current window") or "current window", task.get("button", "")
            )
            return str(res.get("message", "")) or "Clicked it."

        if action == "press_keys":
            res = executor.automate_press_keys(
                task.get("window", "current window") or "current window", task.get("keys", "")
            )
            return str(res.get("message", "")) or "Sent the keys."

        if action == "device_task":
            # Anything else the user could do — hand to the executor's general
            # orchestrator (deterministic command shortcuts + planner).
            res = executor.general({"raw_transcript": task.get("task", "") or task.get("title", "")})
            return str(res.get("message", "")) or "Done."

        if action == "answer":
            spoken = task.get("speak") or task.get("message") or ""
            if spoken:
                self._speak_line(spoken)
            return spoken or "Done."

        # Unknown action → fall back to the general orchestrator rather than skip.
        res = executor.general({"raw_transcript": task.get("task") or task.get("title") or action})
        return str(res.get("message", "")) or f"Handled: {action}"


# Module-level singleton
orchestrator = Orchestrator()
