"""Desktop launcher for pCloud Sync.

Starts the server in the background, then opens a native window. An icon
stays in the notification area to reopen the window or quit.

If the UI libraries are not installed, the application falls back to the
default browser: it stays usable in every case.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

# pythonw.exe starts without a console: sys.stdout and sys.stderr are None.
# Any library that writes to them - uvicorn installs a log handler on stdout
# at import time - then raises an exception nothing can display, and the
# application dies without a word. Give them a silent output before anything
# tries to write.
if sys.stdout is None or sys.stderr is None:
    _void = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _void
    if sys.stderr is None:
        sys.stderr = _void

BASE = Path(__file__).parent
ICON = BASE / "app" / "static" / "icon.png"

log = logging.getLogger("pcloud-sync.desktop")


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _wait_for_server(host: str, port: int, timeout: float = 25.0) -> bool:
    target = "127.0.0.1" if host == "0.0.0.0" else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(target, port):
            return True
        time.sleep(0.25)
    return False


def _who_is_there(port: int) -> str:
    """Identifies what holds the port.

    Returns "free", "us" when it is another copy of pCloud Sync, or "other"
    when third-party software holds it. A plain open-port test is not
    enough: it would make the neighbour pass for us.
    """
    if not _port_open("127.0.0.1", port):
        return "free"
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/ping", timeout=2
        ) as response:
            import json

            if json.loads(response.read()).get("app") == "pcloud-sync":
                return "us"
    except Exception:  # noqa: BLE001
        pass
    return "other"


class Tray:
    """Notification-area icon. Silent when pystray is missing."""

    def __init__(self, url: str, on_open, on_quit) -> None:
        self.url = url
        self.on_open = on_open
        self.on_quit = on_quit
        self.icon = None

    def start(self) -> None:
        try:
            import pystray
            from PIL import Image
        except Exception as exc:  # noqa: BLE001
            # pystray raises ValueError, not ImportError, when no display
            # backend is available. Catch wide: the icon is a comfort, never
            # a requirement.
            log.info("No notification icon (%s).", exc)
            return

        try:
            image = Image.open(ICON)
        except OSError:
            return

        menu = pystray.Menu(
            pystray.MenuItem("Open pCloud Sync", lambda: self.on_open(), default=True),
            pystray.MenuItem("Open in the browser", lambda: webbrowser.open(self.url)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.on_quit()),
        )
        self.icon = pystray.Icon("pcloud-sync", image, "pCloud Sync", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass


def run() -> int:
    from app import config as config_module
    from app.db import History
    from app.engine import SyncEngine
    from app.rclone import RcloneEngine, RcloneError
    from app.scheduler import Scheduler
    from app.server import create_app
    from app.store import ProfileStore

    # -- configuration
    try:
        cfg = config_module.load(config_module.ensure_config(BASE / "config.yaml"))
    except config_module.ConfigError as exc:
        _fatal("Invalid configuration", str(exc))
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(cfg.log_dir / "app.log", encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    url = f"http://{'127.0.0.1' if cfg.host == '0.0.0.0' else cfg.host}:{cfg.port}"

    occupant = _who_is_there(cfg.port)
    if occupant == "us":
        webbrowser.open(url)
        return 0
    if occupant == "other":
        _fatal(
            f"Port {cfg.port} is already taken",
            f"Another program is using port {cfg.port} on this machine.\n\n"
            f"Open config.yaml and replace the line\n"
            f"    port: {cfg.port}\n"
            f"with another number, for example {cfg.port + 10}.\n\n"
            f"Then start the application again.",
        )
        return 5

    # -- engine
    rclone = RcloneEngine(cfg.rclone_binary, cfg.rc_port, cfg.log_dir)
    try:
        rclone.start()
    except RcloneError as exc:
        _fatal("rclone not found", str(exc))
        return 3

    store = ProfileStore(cfg.profiles_file)
    store.seed(cfg.seed_profiles)
    history = History(cfg.database)
    engine = SyncEngine(cfg, rclone, history, store)
    scheduler = Scheduler(cfg, engine, store)
    scheduler.start()
    app = create_app(cfg, rclone, engine, history, scheduler, store)

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()

    if not _wait_for_server(cfg.host, cfg.port):
        _fatal("Startup failed", "The internal server did not respond.")
        rclone.stop()
        return 4

    # -- window
    stopping = threading.Event()

    def shutdown() -> None:
        if stopping.is_set():
            return
        stopping.set()
        scheduler.stop()
        rclone.stop()
        history.close()
        server.should_exit = True

    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        log.info("No native window (%s), opening in the browser.", exc)
        # Fallback: open the browser and wait.
        tray = Tray(url, lambda: webbrowser.open(url), lambda: (shutdown(), sys.exit(0)))
        tray.start()
        webbrowser.open(url)
        try:
            while not stopping.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        shutdown()
        return 0

    window = webview.create_window(
        "pCloud Sync",
        url,
        width=1180,
        height=860,
        min_size=(760, 560),
        background_color="#0C1418",
    )

    tray = Tray(
        url,
        on_open=lambda: _show(window),
        on_quit=lambda: (shutdown(), _destroy(window)),
    )
    tray.start()

    try:
        if ICON.exists():
            try:
                webview.start(icon=str(ICON))
            except TypeError:
                # Older pywebview versions do not accept icon=
                webview.start()
        else:
            webview.start()
    except Exception as exc:  # noqa: BLE001
        log.error("The window could not open (%s).", exc)
        webbrowser.open(url)
        try:
            while not stopping.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    finally:
        tray.stop()
        shutdown()

    return 0


def _show(window) -> None:
    try:
        window.show()
        window.restore()
    except Exception:  # noqa: BLE001
        pass


def _destroy(window) -> None:
    try:
        window.destroy()
    except Exception:  # noqa: BLE001
        pass


def _fatal(title: str, message: str) -> None:
    """Shows an error, in a dialog box when possible."""
    text = f"{title}\n\n{message}"
    print(text, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, f"pCloud Sync - {title}", 0x10)
        except Exception:  # noqa: BLE001
            pass


def _install_crash_handler() -> None:
    """Without a console, an uncaught exception vanishes without a trace."""

    def handler(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            report = BASE / "startup-error.log"
            report.write_text(text, encoding="utf-8")
        except OSError:
            report = None
        _fatal(
            "Startup error",
            f"{exc}\n\nDetails saved to:\n{report}" if report else str(exc),
        )

    sys.excepthook = handler


if __name__ == "__main__":
    _install_crash_handler()
    sys.exit(run())
