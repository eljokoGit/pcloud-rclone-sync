"""Desktop launcher for pCloud Sync.

Opens the native window immediately on a startup screen, then initialises
everything else in the background while the screen reports the real phase
in progress. Measured before this order existed: the window was the last
step of a 4.3 s warm startup (cold: much worse) although it depends on
nothing — the application looked dead and users clicked the icon again.

An icon stays in the notification area to reopen the window or quit. If
the UI libraries are not installed, the application falls back to the
default browser: it stays usable in every case.
"""

from __future__ import annotations

import json
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
SPLASH = BASE / "app" / "static" / "splash.html"

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
        time.sleep(0.05)
    return False


def _wait_port_free(port: int, timeout: float = 15.0) -> None:
    """After /api/restart the parent process is still releasing the port."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_open("127.0.0.1", port, timeout=0.1):
            return
        time.sleep(0.2)


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


class Boot:
    """Background initialisation, reporting each real phase to the window.

    No invented progress bar: the status line names what the machine is
    actually doing, and only changes when the phase changes.
    """

    def __init__(self, window) -> None:
        self.window = window
        self.url: str | None = None
        self.tray: Tray | None = None
        self.stopping = threading.Event()
        self.failed = False
        self._last_status = ""
        self._scheduler = None
        self._rclone = None
        self._history = None
        self._server = None

    # -- window messages -------------------------------------------------------

    def _js(self, call: str) -> None:
        try:
            self.window.evaluate_js(call)
        except Exception:  # noqa: BLE001
            # The window may not have finished loading the splash yet;
            # push_last() replays the current status once it is shown.
            pass

    def status(self, text: str) -> None:
        self._last_status = text
        self._js(f"setStatus({json.dumps(text)})")

    def push_last(self) -> None:
        if self._last_status:
            self._js(f"setStatus({json.dumps(self._last_status)})")

    def error(self, title: str, detail: str) -> None:
        self.failed = True
        self._js(f"setError({json.dumps(title)}, {json.dumps(detail)})")

    # -- startup sequence ------------------------------------------------------

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001
            log.exception("Startup failed.")
            self.error("Startup error", str(exc))

    def _run(self) -> None:
        # The heavy imports (FastAPI alone costs ~0.8 s) happen here, behind
        # the already-visible window, not before it.
        self.status("loading application modules")
        from app import config as config_module
        from app.db import History
        from app.engine import SyncEngine
        from app.rclone import RcloneEngine, RcloneError
        from app.scheduler import Scheduler
        from app.server import create_app
        from app.store import ProfileStore
        import uvicorn

        try:
            cfg = config_module.load(config_module.ensure_config(BASE / "config.yaml"))
        except config_module.ConfigError as exc:
            self.error("Invalid configuration", str(exc))
            return

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
            datefmt="%H:%M:%S",
            handlers=[logging.FileHandler(cfg.log_dir / "app.log", encoding="utf-8")],
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)

        self.url = f"http://{'127.0.0.1' if cfg.host == '0.0.0.0' else cfg.host}:{cfg.port}"

        if os.environ.get("PCLOUDSYNC_RESTART"):
            self.status("waiting for the previous instance to close")
            _wait_port_free(cfg.port)

        self.status("checking ports")
        occupant = _who_is_there(cfg.port)
        if occupant == "us":
            # Another live copy: this window becomes its interface.
            self.window.load_url(self.url)
            return
        if occupant == "other":
            self.error(
                f"Port {cfg.port} is already taken",
                f"Another program is using port {cfg.port} on this machine. "
                f"Open config.yaml, replace “port: {cfg.port}” with another "
                f"number (for example {cfg.port + 10}), then start again.",
            )
            return
        if self.stopping.is_set():
            return

        self.status("starting the rclone engine")
        rclone = RcloneEngine(cfg.rclone_binary, cfg.rc_port, cfg.log_dir)
        try:
            rclone.start()
        except RcloneError as exc:
            self.error("rclone not found", str(exc))
            return
        self._rclone = rclone
        if self.stopping.is_set():
            return

        self.status("loading profiles and history")
        store = ProfileStore(cfg.profiles_file)
        store.seed(cfg.seed_profiles)
        history = History(cfg.database)
        self._history = history

        self.status("restoring plans")
        engine = SyncEngine(cfg, rclone, history, store)
        scheduler = Scheduler(cfg, engine, store)
        scheduler.start()
        self._scheduler = scheduler

        self.status("starting the interface")
        app = create_app(cfg, rclone, engine, history, scheduler, store)
        server = uvicorn.Server(
            uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning")
        )
        self._server = server
        threading.Thread(target=server.run, daemon=True).start()
        if not _wait_for_server(cfg.host, cfg.port):
            self.error("Startup failed", "The internal server did not respond.")
            return

        self.tray = Tray(
            self.url,
            on_open=lambda: _show(self.window),
            on_quit=lambda: (self.shutdown(), _destroy(self.window)),
        )
        self.tray.start()

        if not self.stopping.is_set():
            self.window.load_url(self.url)

    def shutdown(self) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        if self.tray is not None:
            self.tray.stop()
        if self._scheduler is not None:
            self._scheduler.stop()
        if self._rclone is not None:
            self._rclone.stop()
        if self._history is not None:
            self._history.close()
        if self._server is not None:
            self._server.should_exit = True


def run() -> int:
    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        log.info("No native window (%s), falling back to the browser.", exc)
        return _run_in_browser()

    try:
        splash = SPLASH.read_text(encoding="utf-8")
    except OSError:
        splash = "<html><body style='background:#0C1418'></body></html>"

    window = webview.create_window(
        "pCloud Sync",
        html=splash,
        width=1180,
        height=860,
        min_size=(760, 560),
        background_color="#0C1418",
    )

    boot = Boot(window)
    # The splash page may not be rendered yet when the first statuses are
    # pushed; replay the current one as soon as the window exists.
    window.events.shown += boot.push_last
    threading.Thread(target=boot.run, daemon=True).start()

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
        deadline = time.time() + 30
        while boot.url is None and not boot.failed and time.time() < deadline:
            time.sleep(0.2)
        if boot.url:
            webbrowser.open(boot.url)
            try:
                while not boot.stopping.is_set():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
    finally:
        boot.shutdown()

    return 0


def _run_in_browser() -> int:
    """Sequential fallback when pywebview is unavailable."""
    from app import config as config_module
    from app.db import History
    from app.engine import SyncEngine
    from app.rclone import RcloneEngine, RcloneError
    from app.scheduler import Scheduler
    from app.server import create_app
    from app.store import ProfileStore
    import uvicorn

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

    if os.environ.get("PCLOUDSYNC_RESTART"):
        _wait_port_free(cfg.port)

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

    server = uvicorn.Server(
        uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()

    if not _wait_for_server(cfg.host, cfg.port):
        _fatal("Startup failed", "The internal server did not respond.")
        rclone.stop()
        return 4

    stopping = threading.Event()

    def shutdown() -> None:
        if stopping.is_set():
            return
        stopping.set()
        scheduler.stop()
        rclone.stop()
        history.close()
        server.should_exit = True

    tray = Tray(url, lambda: webbrowser.open(url), lambda: (shutdown(), sys.exit(0)))
    tray.start()
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
