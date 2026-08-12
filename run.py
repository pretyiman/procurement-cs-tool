"""Standalone launcher (Phase 11): start the app and open the browser to
it. This is the entry point PyInstaller packages, not app/main.py directly
- keeps the "how do we start this thing" concern out of the FastAPI app
itself.
"""

import threading
import webbrowser

import uvicorn

from app.main import app

HOST = "127.0.0.1"
PORT = 8000


def _open_browser() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}")


def main() -> None:
    threading.Timer(1.5, _open_browser).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
