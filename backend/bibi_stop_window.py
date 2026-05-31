"""Close the Bibi app window(s) by title — never touches the user's main Brave."""
import time
try:
    import pygetwindow as gw
    for _ in range(4):
        wins = [w for w in gw.getWindowsWithTitle("Voice Assistant")]
        if not wins:
            break
        for w in wins:
            try:
                w.close()
            except Exception:
                pass
        time.sleep(0.4)
except Exception:
    pass
