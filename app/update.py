"""Update check and self-update from GitHub releases.

The check asks the releases API for the latest tag and compares it with the
running version. Applying an update downloads the release zip, validates its
content, backs up the files it is about to replace, and extracts over the
installation folder. The application is not restarted: replacing .py and
static files on disk is safe while running (Python reads them once at
import), and the user finishes the update with a normal restart.
"""

from __future__ import annotations

import io
import logging
import re
import ssl
import threading
import time
import zipfile
from pathlib import Path

import httpx

log = logging.getLogger("pcloud-sync.update")

REPO = "eljokoGit/pcloud-rclone-sync"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
_PREFIX = "pcloud-rclone-sync/"
_CACHE_SECONDS = 6 * 3600

# Never overwritten by an update: the user's own files and local state.
_PROTECTED_FILES = {"config.yaml"}
_PROTECTED_DIRS = {"runtime", "logs"}


def _ssl_context() -> ssl.SSLContext:
    # The default certifi bundle fails behind corporate TLS interception,
    # whose root certificate lives in the OS store — browsers work on such
    # machines for exactly that reason. ssl.create_default_context() loads
    # the Windows store, so the check works wherever a browser does.
    return ssl.create_default_context()


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=30.0,
        verify=_ssl_context(),
        follow_redirects=True,
        headers={"User-Agent": "pcloud-rclone-sync"},
    )


def _version_tuple(text: str) -> tuple:
    """'v1.2.3' or '1.2.3' -> (1, 2, 3); anything unreadable -> ()."""
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:3])


class Updater:
    def __init__(self, install_dir: Path, current: str) -> None:
        self.install_dir = Path(install_dir)
        self.current = current
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cache: dict | None = None
        self._checked_at = 0.0
        # idle -> downloading -> installing -> done | error
        self.status: dict = {"state": "idle", "detail": ""}

    # -- check -------------------------------------------------------------

    def check(self, force: bool = False) -> dict:
        with self._lock:
            fresh = self._cache is not None and time.time() - self._checked_at < _CACHE_SECONDS
            if fresh and not force:
                return dict(self._cache)

        try:
            with _client() as client:
                response = client.get(API_LATEST)
                response.raise_for_status()
                data = response.json()
            tag = str(data.get("tag_name") or "")
            asset = next(
                (a for a in data.get("assets", [])
                 if str(a.get("name", "")).endswith(".zip")),
                None,
            )
            info = {
                "current": self.current,
                "latest": tag.lstrip("v"),
                "available": (
                    asset is not None
                    and _version_tuple(tag) > _version_tuple(self.current)
                ),
                "url": data.get("html_url", ""),
                "asset_url": asset.get("browser_download_url") if asset else None,
                "error": "",
            }
        except Exception as exc:  # noqa: BLE001
            # The check must never bother the user: no network, an API rate
            # limit or an uncooperative TLS interceptor simply mean
            # "unknown", and the interface shows nothing.
            log.info("Update check failed: %s", exc)
            info = {
                "current": self.current, "latest": "", "available": False,
                "url": "", "asset_url": None, "error": str(exc),
            }

        with self._lock:
            self._cache = info
            self._checked_at = time.time()
        return dict(info)

    # -- apply -------------------------------------------------------------

    def start_apply(self) -> bool:
        """Starts the update in the background. False when nothing to do."""
        info = self.check()
        with self._lock:
            if not info.get("available") or not info.get("asset_url"):
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            self.status = {"state": "downloading", "detail": ""}
            self._thread = threading.Thread(
                target=self._apply,
                args=(info["asset_url"], info["latest"]),
                daemon=True,
            )
            self._thread.start()
        return True

    def _apply(self, asset_url: str, version: str) -> None:
        try:
            with _client() as client:
                response = client.get(asset_url)
                response.raise_for_status()
                payload = response.content

            self.status = {"state": "installing", "detail": ""}
            archive = zipfile.ZipFile(io.BytesIO(payload))
            names = archive.namelist()

            # The archive is fetched over TLS from our own releases, but it
            # is still validated before a single byte lands on disk: the
            # expected layout, no path escaping the prefix.
            if f"{_PREFIX}run.py" not in names:
                raise ValueError("The archive does not look like a pCloud Sync release.")
            for name in names:
                if not name.startswith(_PREFIX) or ".." in name or name.startswith("/"):
                    raise ValueError(f"Unexpected path in the archive: {name}")

            backup_dir = self.install_dir / f"update-backup-{self.current}"
            for name in names:
                rel = name[len(_PREFIX):]
                if not rel or name.endswith("/"):
                    continue
                top = rel.split("/", 1)[0]
                if rel in _PROTECTED_FILES or top in _PROTECTED_DIRS or top.startswith("update-backup"):
                    continue
                target = self.install_dir / rel
                if target.exists():
                    keep = backup_dir / rel
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    if not keep.exists():
                        keep.write_bytes(target.read_bytes())
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))

            log.info("Update %s installed; previous files kept in %s.", version, backup_dir.name)
            self.status = {
                "state": "done",
                "detail": f"Version {version} installed. Restart the application to finish.",
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("Update failed: %s", exc)
            self.status = {"state": "error", "detail": f"Update failed: {exc}"}
