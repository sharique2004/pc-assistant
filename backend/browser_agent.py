"""
browser_agent.py — Bibi's hands on YOUR real Brave.

Per the user's choice, Bibi controls their actual Brave (real profile, logins,
and restored tabs).  It launches Brave in "control mode" (a remote-debugging
port) using the real user-data-dir, then attaches over the Chrome DevTools
Protocol with Playwright.  From there it drives the REAL tabs the user sees:
clicks "the second video" on the YouTube page they're on, types in a site's
search box, etc.  Real cursor movement, real clicks.

If a normal Brave is already open (without the control port), Bibi can't attach
— it reports a clear message asking the user to close Brave once so Bibi can
reopen it in control mode (their tabs are restored via --restore-last-session).

OPEN-ONLY: never closes the user's tabs.  On shutdown it only detaches the CDP
connection; the user's Brave stays open.

Python 3.11+
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CDP_PORT = int(os.getenv("BIBI_CDP_PORT", "9222"))
_CDP_ENDPOINT = f"http://127.0.0.1:{_CDP_PORT}"

SITE_CONFIG: dict[str, dict] = {
    "youtube": {
        "domain": "youtube.com",
        "home": "https://www.youtube.com",
        "search_url": "https://www.youtube.com/results?search_query={q}",
        "result_selectors": [
            "a#video-title", "ytd-video-renderer a#video-title",
            "ytd-rich-item-renderer a#video-title-link", "a#video-title-link",
        ],
    },
    "netflix": {
        "domain": "netflix.com",
        "home": "https://www.netflix.com",
        "search_url": "https://www.netflix.com/search?q={q}",
        "result_selectors": ["a.slider-refocus", ".title-card a", "[data-uia='title-card'] a", ".slider-item a"],
    },
    "crunchyroll": {
        "domain": "crunchyroll.com",
        "home": "https://www.crunchyroll.com",
        "search_url": "https://www.crunchyroll.com/search?q={q}",
        "result_selectors": ["a[href*='/series/']", "[data-t='search-result'] a", ".browse-card a", "a.card"],
    },
    "prime": {
        "domain": "primevideo.com",
        "home": "https://www.primevideo.com",
        "search_url": "https://www.primevideo.com/search/ref=atv_nb_sr?phrase={q}",
        "result_selectors": ["a[href*='/detail/']", "[data-testid='card'] a", "article a", "li a[href*='/detail/']"],
    },
}

_SITE_ALIASES = {
    "youtube": "youtube", "yt": "youtube", "netflix": "netflix",
    "crunchyroll": "crunchyroll", "crunchy": "crunchyroll", "cr": "crunchyroll",
    "prime": "prime", "prime video": "prime", "primevideo": "prime", "amazon prime": "prime",
}

# Generic in-page search-box candidates (for "search X inside this site").
_SEARCH_BOX_SELECTORS = [
    "input[type='search']",
    "input[name*='search' i]", "input[id*='search' i]",
    "input[placeholder*='search' i]", "input[aria-label*='search' i]",
    "input[role='searchbox']", "[role='searchbox']",
    "input[type='text']",
]


def normalize_site(name: str) -> str:
    return _SITE_ALIASES.get(str(name or "").strip().lower(), str(name or "").strip().lower())


def _resolve_brave_path() -> str:
    configured = os.getenv("BRAVE_PATH", "").strip().strip('"')
    if configured and Path(configured).exists():
        return configured
    for path in (
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ):
        if path and Path(path).exists():
            return path
    return ""


def _brave_user_data_dir() -> str:
    configured = os.getenv("BRAVE_USER_DATA_DIR", "").strip().strip('"')
    if configured:
        return configured
    return os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data")


def _cdp_up(timeout: float = 1.0) -> bool:
    try:
        urllib.request.urlopen(f"{_CDP_ENDPOINT}/json/version", timeout=timeout)
        return True
    except Exception:
        return False


def _brave_running() -> bool:
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == "brave.exe":
                return True
    except Exception:
        pass
    return False


class BrowserCockpit:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._cmds: "queue.Queue[dict]" = queue.Queue()
        self._running = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._png: bytes | None = None
        self._png_lock = threading.Lock()
        self._current_url = "about:blank"
        self._current_title = ""
        self._tab_count = 0
        self._error = ""
        self._refresh_s = float(os.getenv("BIBI_BROWSER_REFRESH_S", "0.6"))
        self._viewport = (int(os.getenv("BIBI_BROWSER_W", "1280")),
                          int(os.getenv("BIBI_BROWSER_H", "800")))

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> dict:
        with self._lock:
            if self._running.is_set():
                return self.status()
            self._running.set()
            self._ready.clear()
            self._error = ""
            self._thread = threading.Thread(target=self._loop, daemon=True, name="brave-cockpit")
            self._thread.start()
        self._ready.wait(timeout=float(os.getenv("BIBI_BROWSER_BOOT_S", "40")))
        return self.status()

    def stop(self) -> dict:
        self._running.clear()
        self._cmds.put({"op": "__stop__"})
        return {"running": False}

    def is_running(self) -> bool:
        return self._running.is_set()

    # ── public commands ──────────────────────────────────────────────────
    def open_browser(self, wait: bool = True, timeout: float = 45.0) -> dict:
        """Launch the user's real Brave in control mode (or confirm it's up)."""
        self.start()
        if self._error:
            return {"ok": False, "error": self._error}
        return {"ok": self._ready.is_set(), "error": self._error}

    def open_url(self, url: str, new_tab: bool = True, wait: bool = False, timeout: float = 30.0) -> dict:
        return self._submit({"op": "open", "url": _normalize_url(url), "new_tab": new_tab}, wait, timeout)

    def search_site(self, site: str, query: str, wait: bool = True, timeout: float = 40.0) -> dict:
        return self._submit({"op": "search", "site": normalize_site(site), "query": query}, wait, timeout)

    def play_result(self, site: str = "", index: int = 1, title: str = "",
                    wait: bool = True, timeout: float = 40.0) -> dict:
        return self._submit(
            {"op": "play", "site": normalize_site(site), "index": int(index or 1), "title": title}, wait, timeout)

    def search_here(self, query: str, wait: bool = True, timeout: float = 30.0) -> dict:
        return self._submit({"op": "search_here", "query": query}, wait, timeout)

    def act(self, action: str, **kwargs: Any) -> dict:
        return self._submit({"op": action, **kwargs}, wait=False, timeout=20.0)

    def get_screenshot(self) -> bytes | None:
        with self._png_lock:
            return self._png

    def status(self) -> dict:
        return {
            "running": self._running.is_set(), "ready": self._ready.is_set(),
            "url": self._current_url, "title": self._current_title,
            "tabs": self._tab_count, "error": self._error,
        }

    # ── internals ────────────────────────────────────────────────────────
    def _submit(self, cmd: dict, wait: bool, timeout: float) -> dict:
        if not self._running.is_set():
            self.start()
        if self._error and not self._ready.is_set():
            return {"ok": False, "error": self._error}
        if wait:
            done = threading.Event()
            holder: dict = {}
            cmd["_done"] = done
            cmd["_result"] = holder
            self._cmds.put(cmd)
            if done.wait(timeout):
                return holder.get("result", {"ok": True})
            return {"ok": False, "error": "timed out"}
        self._cmds.put(cmd)
        return {"queued": True, "action": cmd.get("op")}

    def _ensure_brave(self) -> bool:
        """Make sure a control-enabled Brave is reachable on the CDP port,
        launching the user's real Brave (their profile + restored tabs) if
        needed.  Returns False with a clear error if it can't attach."""
        if _cdp_up():
            return True
        brave = _resolve_brave_path()
        if not brave:
            self._error = "I couldn't find Brave. Set BRAVE_PATH in .env."
            return False
        user_data = _brave_user_data_dir()
        try:
            subprocess.Popen(
                [brave, f"--remote-debugging-port={_CDP_PORT}",
                 f"--user-data-dir={user_data}", "--restore-last-session"],
                shell=False,
            )
        except Exception as exc:  # noqa: BLE001
            self._error = f"I couldn't launch Brave: {exc}"
            return False
        deadline = time.time() + float(os.getenv("BIBI_BROWSER_BOOT_S", "40")) - 3
        while time.time() < deadline:
            if _cdp_up():
                return True
            time.sleep(0.7)
        # Couldn't enable control — almost always because a normal Brave is open.
        if _brave_running():
            self._error = ("Brave is open without control mode. Please close your Brave "
                           "window once and say “open my browser” — I'll reopen it with your tabs.")
        else:
            self._error = "I couldn't start Brave in control mode. Try again, or open it via “open my browser.”"
        return False

    def _loop(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            self._error = f"Playwright not available: {exc}"
            self._running.clear(); self._ready.set(); return

        try:
            with sync_playwright() as p:
                if not self._ensure_brave():
                    self._running.clear(); self._ready.set()
                    logger.warning("Brave control not available: %s", self._error)
                    return
                browser = p.chromium.connect_over_cdp(_CDP_ENDPOINT)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                active = self._pick_page(context, "")
                self._tab_count = len(context.pages)
                self._capture(active)
                self._ready.set()
                logger.info("Attached to your Brave over CDP (%d tabs).", self._tab_count)

                last_shot = 0.0
                while self._running.is_set():
                    try:
                        cmd = self._cmds.get(timeout=self._refresh_s)
                    except queue.Empty:
                        cmd = None
                    if cmd:
                        if cmd.get("op") == "__stop__":
                            break
                        active = self._handle(context, active, cmd) or active
                    now = time.time()
                    if now - last_shot >= self._refresh_s:
                        self._capture(active)
                        last_shot = now
                try:
                    browser.close()  # detach CDP only; user's Brave stays open
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            logger.exception("Brave cockpit error")
        finally:
            self._running.clear(); self._ready.set()

    def _finish(self, cmd: dict, result: dict) -> None:
        if cmd.get("_result") is not None:
            cmd["_result"]["result"] = result
        if cmd.get("_done") is not None:
            cmd["_done"].set()

    def _pick_page(self, context, site_hint: str):
        pages = [pg for pg in context.pages if not (pg.url or "").startswith("devtools://")]
        if not pages:
            return context.new_page()
        if site_hint:
            dom = SITE_CONFIG.get(site_hint, {}).get("domain", site_hint)
            for pg in pages:
                if dom and dom in (pg.url or ""):
                    return pg
        return pages[-1]

    def _handle(self, context, active, cmd):
        op = cmd.get("op")
        try:
            if op == "open":
                page = context.new_page() if cmd.get("new_tab", True) else active
                page.goto(cmd["url"], wait_until="domcontentloaded", timeout=25000)
                self._tab_count = len(context.pages)
                self._capture(page); self._finish(cmd, {"ok": True, "url": page.url})
                return page

            if op == "search":
                site = cmd["site"]
                page = self._pick_page(context, site)
                page = self._site_search(context, page, site, cmd["query"])
                self._capture(page); self._finish(cmd, {"ok": True, "url": page.url})
                return page

            if op == "search_here":
                page = active
                ok = self._search_in_page(page, cmd.get("query", ""))
                self._capture(page); self._finish(cmd, {"ok": ok})
                return page

            if op == "play":
                site = cmd.get("site", "")
                page = self._pick_page(context, site)
                if site and SITE_CONFIG.get(site, {}).get("domain", site) not in (page.url or ""):
                    if cmd.get("title"):
                        page = self._site_search(context, page, site, cmd["title"])
                    else:
                        page = self._goto_home(page, site)
                ok = self._click_result(page, site, cmd.get("index", 1), cmd.get("title", ""))
                self._capture(page); self._finish(cmd, {"ok": ok})
                return page

            if op == "click_text":
                ok = self._click_by_text(active, cmd.get("text", ""))
                self._capture(active); self._finish(cmd, {"ok": ok}); return active
            if op == "click":
                vw, vh = active.viewport_size["width"], active.viewport_size["height"]
                self._move_and_click(active, float(cmd.get("x", 0.5)) * vw, float(cmd.get("y", 0.5)) * vh)
                return active
            if op == "type":
                active.keyboard.type(str(cmd.get("text", "")), delay=20); return active
            if op == "key":
                active.keyboard.press(str(cmd.get("key", "Enter"))); return active
            if op == "scroll":
                active.mouse.wheel(0, float(cmd.get("dy", 500))); return active
        except Exception as exc:  # noqa: BLE001
            logger.debug("cockpit cmd '%s' failed: %s", op, exc)
            self._finish(cmd, {"ok": False, "error": str(exc)})
        return active

    # ── media + in-page helpers ───────────────────────────────────────────
    def _goto_home(self, page, site):
        cfg = SITE_CONFIG.get(site)
        if cfg:
            page.goto(cfg["home"], wait_until="domcontentloaded", timeout=25000)
        return page

    def _site_search(self, context, page, site, query):
        cfg = SITE_CONFIG.get(site)
        q = urllib.parse.quote_plus(str(query or "").strip())
        if not cfg:
            # Unknown site → try the on-page search box, else Google.
            if self._search_in_page(page, query):
                return page
            page.goto(f"https://www.google.com/search?q={q}", wait_until="domcontentloaded", timeout=25000)
            return page
        page.goto(cfg["search_url"].format(q=q), wait_until="domcontentloaded", timeout=25000)
        try:
            page.wait_for_timeout(1300)
        except Exception:
            pass
        return page

    def _search_in_page(self, page, query) -> bool:
        """Type a query into the current page's own search box and submit —
        for 'search X inside this website'."""
        query = str(query or "").strip()
        if not query:
            return False
        for sel in _SEARCH_BOX_SELECTORS:
            try:
                box = page.query_selector(sel)
                if not box:
                    continue
                bx = box.bounding_box()
                if not bx or bx["width"] < 60:
                    continue
                self._move_and_click(page, bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2)
                try:
                    box.fill("")
                except Exception:
                    pass
                page.keyboard.type(query, delay=25)
                page.keyboard.press("Enter")
                page.wait_for_timeout(1200)
                return True
            except Exception:
                continue
        return False

    def _visible_results(self, page, site):
        cfg = SITE_CONFIG.get(site, {})
        selectors = cfg.get("result_selectors", []) + ["a[href]"]
        for sel in selectors:
            try:
                handles = page.query_selector_all(sel)
            except Exception:
                continue
            vis = []
            for h in handles:
                try:
                    box = h.bounding_box()
                    if box and box["width"] > 80 and box["height"] > 45 and box["y"] > 40:
                        vis.append((h, box))
                except Exception:
                    continue
            if vis:
                vis.sort(key=lambda hb: (round(hb[1]["y"] / 60), hb[1]["x"]))
                return [h for h, _ in vis]
        return []

    def _click_result(self, page, site, index, title) -> bool:
        if title and self._click_by_text(page, title):
            return True
        results = self._visible_results(page, site)
        if not results:
            return False
        i = min(max(1, int(index or 1)) - 1, len(results) - 1)
        target = results[i]
        try:
            target.scroll_into_view_if_needed(timeout=4000)
            box = target.bounding_box()
            if box:
                self._move_and_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                target.click(timeout=4000)
            return True
        except Exception:
            try:
                target.click(timeout=3000); return True
            except Exception:
                return False

    def _click_by_text(self, page, text) -> bool:
        text = str(text or "").strip()
        if not text:
            return False
        try:
            loc = page.get_by_text(text, exact=False).first
            loc.scroll_into_view_if_needed(timeout=3000)
            box = loc.bounding_box()
            if box:
                self._move_and_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                loc.click(timeout=3000)
            return True
        except Exception:
            return False

    def _move_and_click(self, page, x: float, y: float) -> None:
        try:
            page.mouse.move(x, y, steps=24)
            page.wait_for_timeout(120)
        except Exception:
            pass
        page.mouse.click(x, y)

    def _capture(self, page) -> None:
        try:
            png = page.screenshot(type="png", timeout=5000)
            with self._png_lock:
                self._png = png
            try:
                self._current_url = page.url
                self._current_title = page.title() or self._current_title
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("screenshot failed: %s", exc)


def _normalize_url(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return "about:blank"
    if u.startswith(("http://", "https://", "about:", "file://")):
        return u
    return "https://" + u


# Module-level singleton
cockpit = BrowserCockpit()
