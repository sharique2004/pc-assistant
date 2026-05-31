"""QA harness: capture screen before, run a typed command through the live
agent, poll to completion, capture after. Prints the result for evaluation."""
import sys, time, json, urllib.request
from pathlib import Path
from PIL import ImageGrab

TMP = Path(__file__).resolve().parent.parent / "tmp"


def _post(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _get(url):
    return json.loads(urllib.request.urlopen(url, timeout=10).read())


def main():
    cmd = sys.argv[1]
    tag = sys.argv[2]
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 70
    ImageGrab.grab().save(str(TMP / f"qa_{tag}_before.png"))
    _post("http://127.0.0.1:5000/agent/run", {"text": cmd})
    wf = {}
    spoken = []
    for _ in range(timeout):
        s = _get("http://127.0.0.1:5000/agent/status")
        wf = s.get("workflow") or {}
        spoken = [x.get("text") for x in (s.get("speak") or [])][-3:]
        if wf.get("status") in ("done", "error"):
            break
        time.sleep(1)
    time.sleep(2.0)  # let the screen settle (page load / app open)
    ImageGrab.grab().save(str(TMP / f"qa_{tag}_after.png"))
    print("CMD:", cmd)
    print("KIND:", wf.get("kind"), "| STATUS:", wf.get("status"))
    print("MESSAGE:", (wf.get("message") or "")[:240])
    print("TASKS:", [(t.get("action"), t.get("status"), (t.get("detail") or "")[:60]) for t in wf.get("tasks", [])])
    print("SPOKEN:", spoken)


if __name__ == "__main__":
    main()
