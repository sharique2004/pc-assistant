"""
Sandbox harness — start the Flask app with `webbrowser.open` stubbed so we can
verify the URL the deep-link path would launch without actually opening a
browser in the headless sandbox.
"""
import os
import sys
import webbrowser

# Capture every URL the executor tries to open.
_OPENED_URLS: list[str] = []


def _fake_open(url, new=0, autoraise=True):
    _OPENED_URLS.append(url)
    return True


webbrowser.open = _fake_open

# Stub os.startfile if executor falls back to it (only exists on Windows).
if hasattr(os, "startfile"):
    def _fake_startfile(path, *args, **kwargs):
        _OPENED_URLS.append(str(path))
    os.startfile = _fake_startfile  # type: ignore[assignment]


# Expose the opened-URL list at a small HTTP route so the test can read it back.
from app import app  # noqa: E402


@app.route("/_sandbox/opened", methods=["GET"])
def sandbox_opened():
    from flask import jsonify
    return jsonify({"opened": list(_OPENED_URLS)})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
