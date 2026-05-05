"""pywebview wrapper: launch TuneHoard in a native window instead of a browser tab.

Usage: python app_native.py
   or: pyinstaller-build with app_native.py as the entry script (alternate to server.py)

This loads server.py (the FastAPI app), starts uvicorn on 127.0.0.1:8765 in a background
thread, then opens a native window pointing at the dashboard. Closing the window stops
the server.
"""

import socket
import threading
import time
import webbrowser

# Neutralize server.py's webbrowser.open() call before we import it — server.py
# pops the user's default browser on startup, but the native pywebview window
# IS the UI here, so a browser tab would just be a duplicate. Monkey-patching is
# per-process and self-contained; no other module relies on webbrowser.open().
webbrowser.open = lambda *args, **kwargs: True

import uvicorn
import webview  # pywebview package — `import webview` not `pywebview`

import server  # imports the FastAPI `app`; server.py runs its module-level setup here


HOST = "127.0.0.1"
PORT = 8765


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll the TCP port until something accepts a connection or we time out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _run_server():
    config = uvicorn.Config(server.app, host=HOST, port=PORT, log_level="warning")
    s = uvicorn.Server(config)
    # Stash the Server object on the function so the close-handler can flip
    # should_exit on it — uvicorn's documented graceful-shutdown hook.
    _run_server.server = s
    s.run()


def main():
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()

    if not _wait_for_server(HOST, PORT, timeout=5.0):
        print("[!] Server failed to start within 5s, exiting.")
        return

    window = webview.create_window(
        title="TuneHoard",
        url=f"http://{HOST}:{PORT}/",
        width=1280,
        height=800,
        resizable=True,
    )

    def on_closed():
        # uvicorn checks should_exit on its event loop and unwinds gracefully.
        # Timing isn't strictly guaranteed, but the thread is daemon=True so the
        # process will exit regardless once main() returns.
        s = getattr(_run_server, "server", None)
        if s is not None:
            s.should_exit = True

    window.events.closed += on_closed
    webview.start()


if __name__ == "__main__":
    main()
